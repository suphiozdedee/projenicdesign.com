from __future__ import annotations

from ccbd.services.project_namespace_runtime.backend import build_backend

from .models import FocusErrorCode, ProjectFocusError


def backend_for_namespace(backend_factory, namespace):
    return build_backend(
        backend_factory,
        socket_path=namespace.tmux_socket_path,
        namespace_state=namespace,
    )


def select_window(backend, *, session_name: str, window_name: str) -> None:
    target = f'{session_name}:{window_name}'
    try:
        cp = backend._tmux_run(['select-window', '-t', target], capture=True, check=False, timeout=0.5)
    except Exception as exc:
        raise ProjectFocusError(FocusErrorCode.TMUX_FOCUS_FAILED, f'failed to select window {window_name}') from exc
    if getattr(cp, 'returncode', 1) != 0:
        raise ProjectFocusError(FocusErrorCode.TARGET_MISSING, f'window {window_name} is not available')


def select_pane(backend, *, pane_id: str) -> None:
    try:
        cp = backend._tmux_run(['select-pane', '-t', pane_id], capture=True, check=False, timeout=0.5)
    except Exception as exc:
        raise ProjectFocusError(FocusErrorCode.TMUX_FOCUS_FAILED, f'failed to select pane {pane_id}') from exc
    if getattr(cp, 'returncode', 1) != 0:
        raise ProjectFocusError(FocusErrorCode.TMUX_FOCUS_FAILED, f'failed to select pane {pane_id}')


def find_agent_pane(backend, *, project_id: str, window_name: str, agent_name: str) -> str | None:
    matches = _list_agent_panes(
        backend,
        {
            '@ccb_project_id': project_id,
            '@ccb_role': 'agent',
            '@ccb_slot': agent_name,
            '@ccb_window': window_name,
            '@ccb_managed_by': 'ccbd',
        },
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        legacy_matches = _list_agent_panes(
            backend,
            {
                '@ccb_project_id': project_id,
                '@ccb_role': 'agent',
                '@ccb_slot': agent_name,
                '@ccb_managed_by': 'ccbd',
            },
        )
        return legacy_matches[0] if len(legacy_matches) == 1 else None
    return None


def refresh_sidebar_panes(backend, *, project_id: str, session_name: str) -> None:
    for pane_id in _list_sidebar_panes(
        backend,
        {
            '@ccb_project_id': project_id,
            '@ccb_role': 'sidebar',
            '@ccb_managed_by': 'ccbd',
        },
        session_name=session_name,
    ):
        _send_sidebar_refresh(backend, pane_id)


def _list_agent_panes(backend, expected: dict[str, str]) -> list[str]:
    return _list_panes_by_options(backend, expected)


def _list_sidebar_panes(backend, expected: dict[str, str], *, session_name: str) -> list[str]:
    return _list_panes_by_options(backend, expected, session_name=session_name)


def _list_panes_by_options(backend, expected: dict[str, str], *, session_name: str | None = None) -> list[str]:
    lister = getattr(backend, 'list_panes_by_user_options', None)
    if callable(lister):
        try:
            candidates = list(lister(expected))
        except Exception:
            return []
        if session_name is None:
            return candidates
        return _filter_panes_by_session(backend, candidates, session_name=session_name)
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return []
    opts = list(expected)
    fields = ['#{pane_id}']
    if session_name is not None:
        fields.append('#{session_name}')
    fields.extend(f'#{{{opt}}}' for opt in opts)
    fmt = '\t'.join(fields)
    try:
        cp = runner(['list-panes', '-a', '-F', fmt], capture=True, check=False, timeout=0.5)
    except Exception:
        return []
    if getattr(cp, 'returncode', 1) != 0:
        return []
    matches: list[str] = []
    for line in (getattr(cp, 'stdout', '') or '').splitlines():
        parts = line.split('\t')
        expected_len = len(opts) + (2 if session_name is not None else 1)
        if len(parts) != expected_len:
            continue
        pane_id = parts[0].strip()
        offset = 1
        if session_name is not None:
            if (parts[1] or '').strip() != session_name:
                continue
            offset = 2
        if not all((parts[index + offset] or '').strip() == expected[opt] for index, opt in enumerate(opts)):
            continue
        if pane_id.startswith('%'):
            matches.append(pane_id)
    return matches


def _filter_panes_by_session(backend, pane_ids: list[str], *, session_name: str) -> list[str]:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return pane_ids
    result: list[str] = []
    for pane_id in pane_ids:
        try:
            cp = runner(
                ['display-message', '-p', '-t', pane_id, '#{session_name}'],
                capture=True,
                check=False,
                timeout=0.5,
            )
        except Exception:
            continue
        if getattr(cp, 'returncode', 1) == 0 and (getattr(cp, 'stdout', '') or '').strip() == session_name:
            result.append(pane_id)
    return result


def _send_sidebar_refresh(backend, pane_id: str) -> None:
    runner = getattr(backend, '_tmux_run', None)
    if not callable(runner):
        return
    try:
        runner(['send-keys', '-t', pane_id, 'C-l'], capture=True, check=False, timeout=0.5)
    except Exception:
        return


__all__ = ['backend_for_namespace', 'find_agent_pane', 'refresh_sidebar_panes', 'select_pane', 'select_window']
