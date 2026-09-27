from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from typing import Mapping

MOBILE_TERMINAL_INITIAL_HISTORY_LINES = 1000
MOBILE_TERMINAL_GEOMETRY_REFRESH_SECONDS = 0.75
HOST_TERMINAL_MAX_SESSIONS = 6
_HOST_TERMINAL_SLOT_RE = re.compile(r'^shell-([1-9][0-9]*)$')


@dataclass(frozen=True)
class TerminalGeometry:
    columns: int = 80
    rows: int = 24
    pixel_width: int = 0
    pixel_height: int = 0

    @classmethod
    def from_mapping(cls, value: object) -> 'TerminalGeometry':
        payload = value if isinstance(value, Mapping) else {}
        return cls(
            columns=_positive_int(payload.get('columns'), 80),
            rows=_positive_int(payload.get('rows'), 24),
            pixel_width=max(0, _int(payload.get('pixel_width'), 0)),
            pixel_height=max(0, _int(payload.get('pixel_height'), 0)),
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            'columns': self.columns,
            'rows': self.rows,
            'pixel_width': self.pixel_width,
            'pixel_height': self.pixel_height,
        }


@dataclass(frozen=True)
class TerminalAttachTarget:
    terminal_id: str
    socket_path: str
    session_name: str
    pane_id: str | None
    geometry: TerminalGeometry
    target_summary: dict[str, object]
    tmux_binary: str = 'tmux'
    include_history: bool = True
    backend_impl: str = 'tmux'
    namespace_ref: dict[str, object] | None = None
    pane_ref: dict[str, object] | None = None
    attach_supported: bool = True
    history_supported: bool = True
    input_supported: bool = True
    blocked_reason: str | None = None

    @property
    def command(self) -> list[str]:
        if self.backend_impl != 'tmux':
            raise RuntimeError(f'terminal command is not available for {self.backend_impl}')
        return _tmux_capture_command(self, self.geometry)


@dataclass(frozen=True)
class _TmuxPaneWindowState:
    window_id: str
    window_columns: int
    window_rows: int
    pane_columns: int
    pane_rows: int
    window_layout: str


@dataclass(frozen=True)
class TerminalHistoryTarget:
    project_id: str
    namespace_epoch: int
    agent: str
    window: str
    pane_id: str
    socket_path: str
    session_name: str
    max_lines: int = 200
    tmux_binary: str = 'tmux'
    backend_impl: str = 'tmux'
    namespace_ref: dict[str, object] | None = None
    pane_ref: dict[str, object] | None = None
    attach_supported: bool = True
    history_supported: bool = True
    input_supported: bool = True
    blocked_reason: str | None = None

    @property
    def command(self) -> list[str]:
        if self.backend_impl != 'tmux':
            raise RuntimeError(f'terminal history command is not available for {self.backend_impl}')
        return [
            self.tmux_binary,
            '-S',
            self.socket_path,
            'capture-pane',
            '-p',
            '-t',
            self.pane_id,
            '-S',
            f'-{max(1, int(self.max_lines))}',
        ]


@dataclass(frozen=True)
class PaneMessageTarget:
    project_id: str
    namespace_epoch: int
    agent: str
    window: str
    pane_id: str
    socket_path: str
    session_name: str
    tmux_binary: str = 'tmux'
    backend_impl: str = 'tmux'
    namespace_ref: dict[str, object] | None = None
    pane_ref: dict[str, object] | None = None
    attach_supported: bool = True
    history_supported: bool = True
    input_supported: bool = True
    blocked_reason: str | None = None


class HostTerminalManager:
    """Owns persistent, device-isolated host shells for CCB Mobile."""

    def __init__(
        self,
        state_dir: Path,
        *,
        home_dir: Path | None = None,
        tmux_binary: str | None = None,
        max_sessions: int = HOST_TERMINAL_MAX_SESSIONS,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.home_dir = Path(home_dir) if home_dir is not None else Path.home()
        self.tmux_binary = str(tmux_binary or shutil.which('tmux') or 'tmux')
        self.max_sessions = max(1, int(max_sessions))
        self.socket_path = self._socket_path()
        self._lock = threading.Lock()

    def attach_target(
        self,
        *,
        terminal_id: str,
        device_id: str,
        client_session_id: str,
        display_name: str,
        geometry: TerminalGeometry,
        include_history: bool,
    ) -> TerminalAttachTarget:
        slot = self._validate_slot(client_session_id)
        device = str(device_id or '').strip()
        if not device:
            raise RuntimeError('host terminal device identity is required')
        session_name = self._session_name(device, slot)
        with self._lock:
            self._ensure_state_dir()
            if not self._has_session(session_name):
                self._create_session(session_name, geometry)
            else:
                self._resize_session(session_name, geometry)
            pane_id = self._pane_id(session_name)
        return TerminalAttachTarget(
            terminal_id=str(terminal_id),
            socket_path=str(self.socket_path),
            session_name=session_name,
            pane_id=pane_id,
            geometry=geometry,
            target_summary={
                'kind': 'host_shell',
                'project_id': '@host',
                'client_session_id': slot,
                'display_name': str(display_name or '').strip() or self._default_name(slot),
                'working_directory': '~',
            },
            tmux_binary=self.tmux_binary,
            include_history=include_history,
        )

    def terminate(self, *, device_id: str, client_session_id: str) -> bool:
        slot = self._validate_slot(client_session_id)
        device = str(device_id or '').strip()
        if not device:
            raise RuntimeError('host terminal device identity is required')
        session_name = self._session_name(device, slot)
        with self._lock:
            self._ensure_state_dir()
            if not self._has_session(session_name):
                return False
            self._run(['kill-session', '-t', session_name])
        return True

    def _validate_slot(self, value: str) -> str:
        slot = str(value or '').strip()
        match = _HOST_TERMINAL_SLOT_RE.fullmatch(slot)
        if match is None or int(match.group(1)) > self.max_sessions:
            raise RuntimeError(
                f'host terminal slot must be shell-1 through shell-{self.max_sessions}'
            )
        return slot

    def _socket_path(self) -> Path:
        preferred_dir = self.state_dir / 'host-terminal'
        preferred = preferred_dir / 'tmux.sock'
        if len(os.fsencode(str(preferred))) < 96:
            return preferred
        digest = hashlib.sha256(str(self.state_dir).encode('utf-8')).hexdigest()[:16]
        user_id = getattr(os, 'getuid', lambda: 0)()
        return Path(tempfile.gettempdir()) / f'ccb-mobile-{user_id}-{digest}' / 'tmux.sock'

    def _ensure_state_dir(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            self.socket_path.parent.chmod(0o700)

    def _session_name(self, device_id: str, slot: str) -> str:
        digest = hashlib.sha256(f'{device_id}\0{slot}'.encode('utf-8')).hexdigest()[:24]
        return f'ccb-mobile-{digest}'

    def _default_name(self, slot: str) -> str:
        return f'Shell {int(slot.rsplit("-", 1)[1])}'

    def _has_session(self, session_name: str) -> bool:
        return self._run(['has-session', '-t', session_name], check=False).returncode == 0

    def _create_session(self, session_name: str, geometry: TerminalGeometry) -> None:
        command = [
            'new-session',
            '-d',
            '-x',
            str(max(20, int(geometry.columns))),
            '-y',
            str(max(5, int(geometry.rows))),
            '-s',
            session_name,
            '-n',
            'shell',
            '-c',
            str(self.home_dir),
        ]
        self._run(command, use_clean_config=True)

    def _resize_session(self, session_name: str, geometry: TerminalGeometry) -> None:
        self._run(
            [
                'resize-window',
                '-t',
                session_name,
                '-x',
                str(max(20, int(geometry.columns))),
                '-y',
                str(max(5, int(geometry.rows))),
            ]
        )

    def _pane_id(self, session_name: str) -> str:
        result = self._run(
            ['display-message', '-p', '-t', f'{session_name}:0.0', '#{pane_id}']
        )
        pane_id = str(result.stdout or '').strip()
        if not pane_id:
            raise RuntimeError('host terminal pane could not be resolved')
        return pane_id

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        use_clean_config: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        prefix = [self.tmux_binary]
        if use_clean_config:
            prefix.extend(('-f', '/dev/null'))
        prefix.extend(('-S', str(self.socket_path)))
        try:
            result = subprocess.run(
                [*prefix, *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=3.0,
                env=_terminal_client_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f'host terminal tmux unavailable: {exc}') from exc
        if check and result.returncode != 0:
            message = (result.stderr or '').strip() or 'host terminal tmux command failed'
            raise RuntimeError(message)
        return result


def capture_tmux_pane_text(
    target: TerminalHistoryTarget,
    *,
    timeout: float = 2.0,
) -> str:
    target = _with_compatible_tmux(target)
    cp = subprocess.run(
        target.command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=max(0.05, float(timeout)),
        env=_terminal_client_env(),
    )
    if cp.returncode != 0:
        message = (cp.stderr or '').strip() or 'tmux capture-pane failed'
        raise RuntimeError(message)
    return _strip_ansi(cp.stdout or '')


def create_tmux_terminal_history(target: TerminalHistoryTarget) -> dict[str, object]:
    text = capture_tmux_pane_text(target)
    return {
        'agent': target.agent,
        'history_scope': 'tmux_scrollback',
        'source_pane_id': target.pane_id,
        'stale': False,
        'blocks': _readable_history_blocks(text),
    }


def send_tmux_pane_message(target: PaneMessageTarget, text: str) -> dict[str, object]:
    target = _with_compatible_tmux(target)
    message = str(text or '')
    _tmux_run(target, ['send-keys', '-t', target.pane_id, 'C-u'])
    _tmux_run(target, ['send-keys', '-t', target.pane_id, '-l', message])
    _tmux_run(target, ['send-keys', '-t', target.pane_id, 'Enter'])
    return {
        'project_id': target.project_id,
        'agent': target.agent,
        'window': target.window,
        'pane_id': target.pane_id,
        'namespace_epoch': target.namespace_epoch,
    }


def _tmux_run(
    target: PaneMessageTarget,
    args: list[str],
) -> None:
    cp = subprocess.run(
        [target.tmux_binary, '-S', target.socket_path, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=2.0,
        env=_terminal_client_env(),
    )
    if cp.returncode != 0:
        message = (cp.stderr or '').strip() or 'tmux send failed'
        raise RuntimeError(message)


class TmuxTerminalSession:
    def __init__(self, target: TerminalAttachTarget) -> None:
        self.target = target
        self._geometry = target.geometry
        self._source_pane_geometry = target.target_summary.get('kind') != 'host_shell'
        self._closed = False
        self._last_snapshot: bytes | None = None
        self._initial_read_complete = False
        self._snapshot_generation = 0
        self._geometry_revision = 0
        self._source_geometry_initialized = False
        self._last_geometry_refresh_monotonic = 0.0
        self._state_lock = threading.Lock()
        self._geometry_refresh_lock = threading.Lock()
        self._pending_projection: dict[str, object] | None = None
        if not target.pane_id:
            raise RuntimeError('terminal target pane evidence is required')

    def viewport_state(self) -> dict[str, object]:
        if self._source_pane_geometry:
            self._refresh_source_geometry(force=True)
        with self._state_lock:
            return {
                'revision': self._geometry_revision,
                'geometry': self._geometry.to_mapping(),
                'resize_policy': (
                    'fixed_source' if self._source_pane_geometry else 'client'
                ),
            }

    def read(self, timeout_seconds: float = 0.1) -> bytes | None:
        if self._source_pane_geometry and self._source_geometry_initialized:
            try:
                self._refresh_source_geometry(force=False)
            except RuntimeError:
                # A transient geometry probe failure must not tear down an
                # otherwise healthy pane stream. The last authoritative grid
                # remains valid until a later probe succeeds.
                pass
        with self._state_lock:
            if self._closed:
                return None
            initial_read_complete = self._initial_read_complete
        if initial_read_complete:
            time.sleep(max(0.0, min(float(timeout_seconds), 0.25)))
        with self._state_lock:
            if self._closed:
                return None
            initial_read = not self._initial_read_complete
            if initial_read:
                self._initial_read_complete = True
            geometry = self._geometry
            previous = self._last_snapshot
            snapshot_generation = self._snapshot_generation
        if initial_read:
            if not self._source_pane_geometry:
                history = _capture_tmux_terminal_pane(
                    self.target,
                    geometry,
                    include_history=self.target.include_history,
                )
                snapshot = _capture_tmux_terminal_pane(
                    self.target,
                    geometry,
                    include_history=False,
                )
                with self._state_lock:
                    if self._closed:
                        return None
                    self._last_snapshot = snapshot
                return _render_terminal_snapshot(history, clear_scrollback=True)
            history = (
                _capture_tmux_terminal_scrollback(self.target, geometry)
                if self.target.include_history
                else b''
            )
            snapshot = _capture_tmux_terminal_pane(
                self.target,
                geometry,
                include_history=False,
            )
            with self._state_lock:
                if self._closed:
                    return None
                self._last_snapshot = snapshot
                if self._source_pane_geometry:
                    self._pending_projection = {
                        'history_reset': True,
                        'history': history,
                        'screen': snapshot,
                    }
            rendered = _join_terminal_projection(history, snapshot)
            return _render_terminal_snapshot(rendered, clear_scrollback=True)
        if previous is None:
            snapshot = _capture_tmux_terminal_pane(
                self.target,
                geometry,
                include_history=False,
            )
            with self._state_lock:
                if self._closed:
                    return None
                self._last_snapshot = snapshot
            return _render_terminal_snapshot(snapshot)
        snapshot = _capture_tmux_terminal_pane(
            self.target,
            geometry,
            include_history=False,
        )
        with self._state_lock:
            if self._closed:
                return None
            if snapshot_generation != self._snapshot_generation:
                self._last_snapshot = snapshot
                if self._source_pane_geometry:
                    self._pending_projection = {'screen': snapshot}
                return _render_terminal_snapshot(snapshot)
            if snapshot == previous:
                return b''
            self._last_snapshot = snapshot
            if self._source_pane_geometry:
                # A fixed source pane and its phone renderer intentionally use
                # different column counts. Source-row cursor deltas are not
                # valid after the phone has locally reflowed those rows, so
                # repaint the visible snapshot without resizing tmux.
                history_append = _terminal_projection_history_append(
                    previous,
                    snapshot,
                )
                self._pending_projection = {
                    'screen': snapshot,
                    **(
                        {'history_append': history_append}
                        if history_append
                        else {}
                    ),
                }
                return _render_terminal_snapshot(snapshot)
            return _render_terminal_delta(
                previous,
                snapshot,
                columns=geometry.columns,
                rows=geometry.rows,
            )

    def take_output_projection(self) -> dict[str, object] | None:
        """Return structured projection metadata for the most recent read.

        Agent terminals mirror a desktop-owned pane.  Their byte stream remains
        available for older clients, while current clients replace the projected
        screen and append only rows that actually scrolled into history.
        """
        with self._state_lock:
            projection = self._pending_projection
            self._pending_projection = None
            return dict(projection) if projection is not None else None

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self.target.pane_id:
            _send_tmux_terminal_bytes(self.target, data)
            return
        os.write(self._master_fd, data)

    def paste(self, text: str) -> None:
        if self.target.pane_id:
            _send_tmux_terminal_literal(self.target, str(text or ''))
            return
        self.write(str(text).encode('utf-8'))

    def resize(self, geometry: TerminalGeometry) -> None:
        if self._source_pane_geometry:
            # Agent panes belong to the desktop CCB tmux namespace. A phone is
            # a viewport over that source grid, never a geometry owner.
            return
        _tmux_terminal_run(
            self.target,
            [
                'resize-window',
                '-t',
                self.target.session_name,
                '-x',
                str(max(20, int(geometry.columns))),
                '-y',
                str(max(5, int(geometry.rows))),
            ],
        )
        with self._state_lock:
            self._geometry = geometry
            self._last_snapshot = None
            self._snapshot_generation += 1
            self._geometry_revision += 1

    def _refresh_source_geometry(self, *, force: bool) -> None:
        now = time.monotonic()
        with self._state_lock:
            if (
                not force
                and now - self._last_geometry_refresh_monotonic
                < MOBILE_TERMINAL_GEOMETRY_REFRESH_SECONDS
            ):
                return
            self._last_geometry_refresh_monotonic = now
        with self._geometry_refresh_lock:
            geometry = _capture_tmux_pane_geometry(self.target)
        with self._state_lock:
            initialized = self._source_geometry_initialized
            self._source_geometry_initialized = True
            if geometry == self._geometry and initialized:
                return
            self._geometry = geometry
            self._last_snapshot = None
            self._snapshot_generation += 1
            self._geometry_revision += 1

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

def create_tmux_terminal_session(target: TerminalAttachTarget) -> TmuxTerminalSession:
    return TmuxTerminalSession(_with_compatible_tmux(target))


def _terminal_client_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env.pop('TMUX', None)
    env.pop('TMUX_PANE', None)
    if not env.get('TERM') or env.get('TERM') == 'dumb':
        env['TERM'] = 'xterm-256color'
    return env


def resolve_tmux_binary(
    socket_path: str,
    session_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    env = _terminal_client_env(environ)
    candidates: list[str] = []
    seen: set[str] = set()
    for directory in os.get_exec_path(env):
        for candidate in _tmux_binary_candidates(directory, env=env):
            if not os.path.isfile(candidate):
                continue
            if os.name != 'nt' and not os.access(candidate, os.X_OK):
                continue
            identity = os.path.realpath(candidate)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(candidate)

    failures: list[str] = []
    for candidate in candidates:
        try:
            cp = subprocess.run(
                [
                    candidate,
                    '-S',
                    socket_path,
                    'has-session',
                    '-t',
                    session_name,
                ],
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=2.0,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f'{candidate}: {exc}')
            continue
        if cp.returncode == 0:
            return candidate
        failures.append(f'{candidate}: {(cp.stderr or "").strip() or "incompatible"}')

    detail = '; '.join(failures) or 'no executable tmux found in PATH'
    raise RuntimeError(f'no compatible tmux client for {session_name}: {detail}')


def _tmux_binary_candidates(directory: str, *, env: Mapping[str, str]) -> tuple[str, ...]:
    base = os.path.join(directory, 'tmux')
    if os.name != 'nt':
        return (base,)
    extensions = [item.strip().lower() for item in str(env.get('PATHEXT') or '').split(os.pathsep) if item.strip()]
    if not extensions:
        extensions = ['.exe', '.cmd', '.bat', '.com']
    candidates = [base]
    candidates.extend(base + extension for extension in extensions)
    return tuple(dict.fromkeys(candidates))


def _with_compatible_tmux(target):
    return replace(
        target,
        tmux_binary=resolve_tmux_binary(target.socket_path, target.session_name),
    )


def _tmux_capture_command(
    target: TerminalAttachTarget,
    geometry: TerminalGeometry,
    *,
    include_history: bool = True,
    history_only: bool = False,
    join_wrapped: bool = False,
) -> list[str]:
    pane_id = str(target.pane_id or '').strip()
    if not pane_id:
        raise RuntimeError('terminal target pane evidence is required')
    history_lines = max(
        MOBILE_TERMINAL_INITIAL_HISTORY_LINES,
        max(1, int(geometry.rows)),
    )
    command = [
        target.tmux_binary,
        '-S',
        target.socket_path,
        'capture-pane',
        '-p',
        '-e',
        '-t',
        pane_id,
    ]
    if join_wrapped:
        command.append('-J')
    if history_only:
        command.extend(('-S', f'-{history_lines}', '-E', '-1'))
    elif include_history:
        command.extend(('-S', f'-{history_lines}'))
    return command


def _capture_tmux_terminal_pane(
    target: TerminalAttachTarget,
    geometry: TerminalGeometry,
    *,
    include_history: bool,
) -> bytes:
    cp = subprocess.run(
        _tmux_capture_command(
            target,
            geometry,
            include_history=include_history,
            join_wrapped=True,
        ),
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=2.0,
        env=_terminal_client_env(),
    )
    if cp.returncode != 0:
        message = (cp.stderr or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(message or 'tmux capture-pane failed')
    return _fit_terminal_snapshot(bytes(cp.stdout or b''), geometry.columns)


def _capture_tmux_terminal_scrollback(
    target: TerminalAttachTarget,
    geometry: TerminalGeometry,
) -> bytes:
    if _capture_tmux_terminal_history_size(target) < 1:
        return b''
    cp = subprocess.run(
        _tmux_capture_command(
            target,
            geometry,
            include_history=False,
            history_only=True,
            join_wrapped=True,
        ),
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=2.0,
        env=_terminal_client_env(),
    )
    if cp.returncode != 0:
        message = (cp.stderr or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(message or 'tmux capture-pane scrollback failed')
    return _fit_terminal_snapshot(bytes(cp.stdout or b''), geometry.columns)


def _capture_tmux_terminal_history_size(target: TerminalAttachTarget) -> int:
    pane_id = str(target.pane_id or '').strip()
    if not pane_id:
        raise RuntimeError('terminal target pane evidence is required')
    cp = subprocess.run(
        [
            target.tmux_binary,
            '-S',
            target.socket_path,
            'display-message',
            '-p',
            '-t',
            pane_id,
            '#{history_size}',
        ],
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=2.0,
        env=_terminal_client_env(),
    )
    if cp.returncode != 0:
        message = (cp.stderr or b'').decode('utf-8', errors='replace').strip()
        raise RuntimeError(message or 'tmux pane history query failed')
    try:
        return max(0, int((cp.stdout or b'0').decode('ascii').strip() or '0'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError('tmux pane history query returned invalid output') from exc


def _capture_tmux_pane_window_state(
    target: TerminalAttachTarget,
) -> _TmuxPaneWindowState:
    pane_id = str(target.pane_id or '').strip()
    if not pane_id:
        raise RuntimeError('terminal target pane evidence is required')
    cp = subprocess.run(
        [
            target.tmux_binary,
            '-S',
            target.socket_path,
            'display-message',
            '-p',
            '-t',
            pane_id,
            '#{window_id}\t#{window_width}\t#{window_height}\t'
            '#{pane_width}\t#{pane_height}\t#{window_layout}',
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=2.0,
        env=_terminal_client_env(),
    )
    if cp.returncode != 0:
        message = (cp.stderr or '').strip() or 'tmux pane geometry query failed'
        raise RuntimeError(message)
    values = str(cp.stdout or '').strip().split('\t', 5)
    if len(values) != 6:
        raise RuntimeError('tmux pane/window geometry query returned invalid output')
    window_id = values[0].strip()
    window_layout = values[5].strip()
    if not window_id or not window_layout:
        raise RuntimeError('tmux pane/window geometry query returned invalid output')
    try:
        window_columns, window_rows, pane_columns, pane_rows = (
            int(value) for value in values[1:5]
        )
    except ValueError as exc:
        raise RuntimeError('tmux pane/window geometry query returned invalid output') from exc
    if min(window_columns, window_rows, pane_columns, pane_rows) < 1:
        raise RuntimeError('tmux pane/window geometry query returned invalid dimensions')
    return _TmuxPaneWindowState(
        window_id=window_id,
        window_columns=window_columns,
        window_rows=window_rows,
        pane_columns=pane_columns,
        pane_rows=pane_rows,
        window_layout=window_layout,
    )


def _capture_tmux_pane_geometry(target: TerminalAttachTarget) -> TerminalGeometry:
    state = _capture_tmux_pane_window_state(target)
    return TerminalGeometry(columns=state.pane_columns, rows=state.pane_rows)


def _fit_terminal_snapshot(snapshot: bytes, columns: int) -> bytes:
    """Normalize line endings without discarding source-pane columns.

    ``columns`` remains in the signature for compatibility with older callers.
    The phone viewport must fit or pan the source grid instead of clipping it.
    """
    del columns
    text = snapshot.decode('utf-8', errors='replace')
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    return '\n'.join(
        _trim_terminal_line_end(line) for line in normalized.split('\n')
    ).encode('utf-8')


_TRAILING_TERMINAL_SPACE_RE = re.compile(
    r'[ \t]{2,}(?=(?:\x1b\[[0-?]*[ -/]*[@-~])*$)'
)


def _trim_terminal_line_end(line: str) -> str:
    return _TRAILING_TERMINAL_SPACE_RE.sub(' ', line)


def _join_terminal_projection(history: bytes, screen: bytes) -> bytes:
    history_text = history.decode('utf-8', errors='replace').rstrip('\n')
    screen_text = screen.decode('utf-8', errors='replace').rstrip('\n')
    if not history_text:
        return screen_text.encode('utf-8')
    if not screen_text:
        return history_text.encode('utf-8')
    return f'{history_text}\n{screen_text}'.encode('utf-8')


def _terminal_projection_history_append(previous: bytes, snapshot: bytes) -> bytes:
    """Detect rows that genuinely scrolled off the top of the source pane."""
    previous_lines = _terminal_snapshot_lines(previous)
    snapshot_lines = _terminal_snapshot_lines(snapshot)
    maximum = min(len(previous_lines), len(snapshot_lines))
    overlap = 0
    for size in range(maximum, 0, -1):
        if previous_lines[-size:] == snapshot_lines[:size]:
            overlap = size
            break
    if overlap == 0:
        return b''
    dropped = previous_lines[:-overlap]
    if not dropped:
        return b''
    return ('\n'.join(dropped).rstrip('\n') + '\n').encode('utf-8')


def _render_terminal_snapshot(
    snapshot: bytes,
    *,
    clear_scrollback: bool = False,
) -> bytes:
    text = snapshot.decode('utf-8', errors='replace').rstrip('\n')
    rendered = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
    clear = b'\x1b[3J' if clear_scrollback else b''
    return b'\x1b[?25l' + clear + b'\x1b[H\x1b[2J' + rendered.encode('utf-8')


def _render_terminal_delta(
    previous: bytes,
    snapshot: bytes,
    *,
    columns: int,
    rows: int,
) -> bytes:
    previous_lines = _terminal_snapshot_lines(previous)
    snapshot_lines = _terminal_snapshot_lines(snapshot)
    first_changed = _first_changed_terminal_line(previous_lines, snapshot_lines)
    if first_changed is None:
        return b''
    previous_visual_rows = _terminal_visual_rows(previous_lines, columns)
    snapshot_visual_rows = _terminal_visual_rows(snapshot_lines, columns)
    if previous_visual_rows > rows or snapshot_visual_rows > rows:
        return _render_terminal_snapshot(snapshot, clear_scrollback=True)
    start_row = _terminal_visual_rows(previous_lines[:first_changed], columns) + 1
    suffix = '\r\n'.join(snapshot_lines[first_changed:]).encode('utf-8')
    return (
        b'\x1b[?25l\x1b[0m'
        + f'\x1b[{start_row};1H\x1b[0J'.encode('ascii')
        + suffix
        + b'\x1b[0m'
    )


def _terminal_snapshot_lines(snapshot: bytes) -> list[str]:
    text = snapshot.decode('utf-8', errors='replace')
    normalized = text.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')
    return normalized.split('\n') if normalized else []


def _first_changed_terminal_line(
    previous_lines: list[str],
    snapshot_lines: list[str],
) -> int | None:
    for index, (before, after) in enumerate(zip(previous_lines, snapshot_lines)):
        if before != after:
            return index
    if len(previous_lines) != len(snapshot_lines):
        return min(len(previous_lines), len(snapshot_lines))
    return None


def _terminal_visual_rows(lines: list[str], columns: int) -> int:
    width = max(1, int(columns))
    rows = 0
    for line in lines:
        display_width = _terminal_display_width(_strip_ansi(line))
        rows += max(1, (display_width + width - 1) // width)
    return rows


def _terminal_display_width(text: str) -> int:
    return sum(_terminal_character_width(character) for character in text)


def _terminal_character_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    if unicodedata.category(character).startswith('C'):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {'F', 'W'} else 1


def _select_tmux_terminal_pane(target: TerminalAttachTarget) -> None:
    _tmux_terminal_run(target, ['select-window', '-t', str(target.pane_id)])
    _tmux_terminal_run(target, ['select-pane', '-t', str(target.pane_id)])


def _send_tmux_terminal_literal(target: TerminalAttachTarget, text: str) -> None:
    if not text:
        return
    _tmux_terminal_run(target, ['send-keys', '-t', str(target.pane_id), '-l', text])


_TMUX_TERMINAL_KEY_NAMES = {
    b'\r\n': 'Enter',
    b'\r': 'Enter',
    b'\n': 'Enter',
    b'\t': 'Tab',
    b'\x1b': 'Escape',
    b'\x01': 'C-a',
    b'\x03': 'C-c',
    b'\x04': 'C-d',
    b'\x05': 'C-e',
    b'\x0b': 'C-k',
    b'\x0c': 'C-l',
    b'\x12': 'C-r',
    b'\x15': 'C-u',
    b'\x17': 'C-w',
    b'\x1a': 'C-z',
    b'\x7f': 'BSpace',
    b'\b': 'BSpace',
    b'\x1b[A': 'Up',
    b'\x1b[B': 'Down',
    b'\x1b[C': 'Right',
    b'\x1b[D': 'Left',
    b'\x1b[H': 'Home',
    b'\x1b[F': 'End',
    b'\x1bOH': 'Home',
    b'\x1bOF': 'End',
    b'\x1b[1~': 'Home',
    b'\x1b[4~': 'End',
    b'\x1b[3~': 'Delete',
    b'\x1b[5~': 'PageUp',
    b'\x1b[6~': 'PageDown',
}
_TMUX_TERMINAL_MULTI_BYTE_KEYS = tuple(
    sorted(
        (value for value in _TMUX_TERMINAL_KEY_NAMES if len(value) > 1),
        key=len,
        reverse=True,
    )
)


def _send_tmux_terminal_bytes(target: TerminalAttachTarget, data: bytes) -> None:
    for kind, value in _parse_tmux_terminal_input(target, data):
        if kind == 'key':
            _tmux_terminal_run(target, ['send-keys', '-t', str(target.pane_id), value])
        else:
            _send_tmux_terminal_literal(target, value)


def _parse_tmux_terminal_input(
    target: TerminalAttachTarget,
    data: bytes,
) -> tuple[tuple[str, str], ...]:
    if not data or _is_terminal_protocol_response(data):
        return ()

    actions: list[tuple[str, str]] = []
    literal_start = 0
    index = 0
    while index < len(data):
        key_bytes = _terminal_key_at(data, index)
        if key_bytes is None:
            byte = data[index]
            if byte < 0x20 or byte == 0x7F:
                raise RuntimeError(f'unsupported terminal input bytes for {target.terminal_id}')
            index += 1
            continue
        if literal_start < index:
            actions.append(('literal', _decode_terminal_literal(target, data[literal_start:index])))
        actions.append(('key', _TMUX_TERMINAL_KEY_NAMES[key_bytes]))
        index += len(key_bytes)
        literal_start = index
    if literal_start < len(data):
        actions.append(('literal', _decode_terminal_literal(target, data[literal_start:])))
    return tuple(actions)


def _terminal_key_at(data: bytes, index: int) -> bytes | None:
    for key_bytes in _TMUX_TERMINAL_MULTI_BYTE_KEYS:
        if data.startswith(key_bytes, index):
            return key_bytes
    candidate = data[index:index + 1]
    if candidate == b'\x1b' and index + 1 < len(data):
        return None
    return candidate if candidate in _TMUX_TERMINAL_KEY_NAMES else None


def _decode_terminal_literal(target: TerminalAttachTarget, data: bytes) -> str:
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise RuntimeError(f'unsupported terminal input bytes for {target.terminal_id}')


_TERMINAL_PROTOCOL_RESPONSES = (
    re.compile(r'\x1b\[\?[0-9;]*c'),  # primary device attributes
    re.compile(r'\x1b\[>[0-9;]*c'),  # secondary device attributes
    re.compile(r'\x1bP!\|[0-9A-Fa-f]*\x1b\\'),  # tertiary device attributes
    re.compile(r'\x1b\[0n'),  # operating status
    re.compile(r'\x1b\[[0-9;]*R'),  # cursor position report
    re.compile(r'\x1b\[8;[0-9]+;[0-9]+t'),  # terminal size report
    re.compile(r'\x1b\[[IO]'),  # focus in/out
    re.compile(r'\x1b\[<[0-9;]+[mM]'),  # SGR mouse report
    re.compile(r'\x1b\]1[01];(?:rgb:)?[0-9A-Fa-f/]+(?:\x07|\x1b\\)'),  # OSC colors
)


def _is_terminal_protocol_response(data: bytes) -> bool:
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return False
    index = 0
    while index < len(text):
        for pattern in _TERMINAL_PROTOCOL_RESPONSES:
            match = pattern.match(text, index)
            if match is not None:
                index = match.end()
                break
        else:
            return False
    return bool(text)


def _tmux_terminal_run(
    target: TerminalAttachTarget,
    args: list[str],
) -> None:
    cp = subprocess.run(
        [target.tmux_binary, '-S', target.socket_path, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=2.0,
        env=_terminal_client_env(),
    )
    if cp.returncode != 0:
        message = (cp.stderr or '').strip() or 'tmux terminal input failed'
        raise RuntimeError(message)


def _readable_history_blocks(text: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    current_type = ''
    current: list[str] = []

    def flush() -> None:
        nonlocal current_type, current
        rendered = '\n'.join(line.rstrip() for line in current).strip()
        if not rendered:
            current_type = ''
            current = []
            return
        block_type = current_type or _classify_line(rendered)
        blocks.append(
            {
                'id': f'history-{len(blocks) + 1}',
                'type': block_type,
                'title': _block_title(block_type),
                'text': rendered,
            }
        )
        current_type = ''
        current = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush()
            continue
        line_type = _classify_line(line)
        if current and line_type != current_type and line_type in {'command', 'diff', 'error'}:
            flush()
        if not current:
            current_type = line_type
        current.append(line)
    flush()
    return blocks


def _classify_line(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith(('$ ', '> ', '# ')):
        return 'command'
    if stripped.startswith(('Traceback ', 'Error:', 'ERROR:', 'Exception:', 'FAILED')):
        return 'error'
    if stripped.startswith(('diff --git ', '+++ ', '--- ', '+ ', '- ', '@@ ')):
        return 'diff'
    if stripped.startswith(('```', 'def ', 'class ', 'import ', 'from ', 'const ', 'final ', 'Future<')):
        return 'code'
    return 'log'


def _block_title(block_type: str) -> str:
    return {
        'command': 'Command',
        'code': 'Code',
        'diff': 'Diff',
        'error': 'Error',
    }.get(block_type, 'Log')


def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)


def _int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _positive_int(value: object, fallback: int) -> int:
    return max(1, _int(value, fallback))
