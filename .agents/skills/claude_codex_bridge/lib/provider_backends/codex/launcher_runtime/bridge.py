from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys

from provider_backends.codex.runtime_artifacts import codex_runtime_artifact_layout
from provider_profiles import load_resolved_provider_profile
from provider_core.transport import endpoint_for_fifo_path

from .command import prepare_codex_home_overrides
from .session_paths import session_file_for_runtime_dir


def post_launch(backend: object, pane_id: str, runtime_dir: Path, launch_session_id: str, prepared_state: dict[str, object]) -> None:
    del launch_session_id
    artifacts = codex_runtime_artifact_layout(runtime_dir)
    write_pane_pid(backend, pane_id, artifacts.codex_pid)
    # design D3 决策 A：herdr 下 bridge 降级为辅助。bridge 依赖 tmux 环境变量
    # （CODEX_TERMINAL=tmux），在 herdr 下 bootstrap 可能失败并阻断 agent launch。
    # 交互 codex CLI 已通过 respawn 进 herdr pane，bridge RPC 为辅助，失败不阻塞。
    if _backend_is_herdr(backend):
        return
    spawn_codex_bridge(runtime_dir=runtime_dir, pane_id=pane_id, prepared_state=prepared_state)
    validate_bridge_bootstrap(runtime_dir)


def _backend_is_herdr(backend: object) -> bool:
    """检测 backend 是否为 herdr-native 后端。

    通过 ``backend_impl`` 类/实例属性显式检测，而非依赖方法存在性判断。
    HerdrBackend 设 ``backend_impl = "herdr"``，TmuxBackend 无此属性。
    """
    return str(getattr(backend, 'backend_impl', '') or '').strip() == 'herdr'


def spawn_codex_bridge(*, runtime_dir: Path, pane_id: str, prepared_state: dict[str, object] | None = None) -> None:
    artifacts = codex_runtime_artifact_layout(runtime_dir)
    env = os.environ.copy()
    # TODO(herdr): CODEX_TERMINAL 应随 backend 选择（herdr→'herdr'，tmux→'tmux'）。
    # 当前硬编码 'tmux' 不影响 herdr pane 内交互 codex CLI（respawn 已进 pane）；
    # bridge 为辅助 RPC，CODEX_TERMINAL 适配留后续 herdr integration。
    env['CODEX_TERMINAL'] = 'tmux'
    env['CODEX_TMUX_SESSION'] = pane_id
    env['CODEX_RUNTIME_DIR'] = str(runtime_dir)
    env['CODEX_INPUT_FIFO'] = str(artifacts.input_fifo)
    env['CODEX_OUTPUT_FIFO'] = str(artifacts.output_fifo)
    env['CODEX_TMUX_LOG'] = str(artifacts.bridge_log)
    for key in (prepared_state or {}).get('codex_app_server_unset_env', ()):
        env.pop(str(key), None)
    env.update(bridge_runtime_env(runtime_dir, prepared_state=prepared_state))
    existing_pythonpath = env.get('PYTHONPATH', '')
    lib_root = str(Path(__file__).resolve().parents[3])
    env['PYTHONPATH'] = lib_root if not existing_pythonpath else lib_root + os.pathsep + existing_pythonpath
    with artifacts.bridge_stdout_log.open('ab') as stdout_log, artifacts.bridge_stderr_log.open('ab') as stderr_log:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'provider_backends.codex.bridge', '--runtime-dir', str(runtime_dir)],
            env=env,
            stdout=stdout_log,
            stderr=stderr_log,
            start_new_session=True,
        )
    artifacts.bridge_pid.write_text(f'{proc.pid}\n', encoding='utf-8')


def bridge_runtime_env(runtime_dir: Path, *, prepared_state: dict[str, object] | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    session_file = session_file_for_runtime_dir(runtime_dir)
    if session_file is not None:
        env['CCB_SESSION_FILE'] = str(session_file)
    profile = load_resolved_provider_profile(runtime_dir)
    env.update(
        prepare_codex_home_overrides(
            runtime_dir,
            profile,
            refresh_home=False,
            enforce_session_namespace=False,
        )
    )
    state = prepared_state or {}
    if bool(state.get('codex_app_server_enabled')):
        env.update({str(key): str(value) for key, value in dict(state.get('codex_app_server_env') or {}).items()})
        env['CCB_CODEX_APP_SERVER_COMMAND_JSON'] = json.dumps(
            list(state.get('codex_app_server_command') or ()),
            ensure_ascii=False,
        )
        env['CCB_CODEX_APP_SERVER_SOCKET'] = str(state.get('codex_app_server_socket') or '')
    return env


def validate_bridge_bootstrap(runtime_dir: Path) -> None:
    artifacts = codex_runtime_artifact_layout(runtime_dir)
    missing: list[str] = []
    if not endpoint_for_fifo_path(artifacts.input_fifo).exists():
        missing.append(str(artifacts.input_fifo.name))
    if not endpoint_for_fifo_path(artifacts.output_fifo).exists():
        missing.append(str(artifacts.output_fifo.name))
    if not artifacts.completion_dir.is_dir():
        missing.append(str(artifacts.completion_dir.name))
    if not artifacts.bridge_log.is_file():
        missing.append(str(artifacts.bridge_log.name))
    if not artifacts.bridge_pid.is_file():
        missing.append(str(artifacts.bridge_pid.name))
    if missing:
        joined = ', '.join(missing)
        raise RuntimeError(f'codex runtime bootstrap missing declared artifacts: {joined}')


def write_pane_pid(backend: object, pane_id: str, path: Path) -> None:
    # herdr backend 通过 respawn 把 codex 交互 CLI 已送进 pane；
    # bridge 为辅助 RPC，PID 缺失不阻塞 launch（design D3 决策 A）。
    if _backend_is_herdr(backend):
        return
    run_fn = getattr(backend, '_tmux_run', None)
    if not callable(run_fn):
        return
    try:
        result = run_fn(
            ['display-message', '-p', '-t', pane_id, '#{pane_pid}'],
            capture=True,
            timeout=1.0,
        )
    except Exception:
        return
    pane_pid = (result.stdout or '').strip()
    if pane_pid.isdigit():
        path.write_text(f'{pane_pid}\n', encoding='utf-8')


__all__ = ['bridge_runtime_env', 'post_launch', 'spawn_codex_bridge', 'validate_bridge_bootstrap', 'write_pane_pid']
