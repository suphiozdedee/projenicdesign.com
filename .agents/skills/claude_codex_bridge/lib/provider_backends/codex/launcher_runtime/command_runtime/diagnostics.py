from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from pathlib import Path


DB_NAME = 'logs_2.sqlite'
TRIGGER_NAME = 'ccb_drop_diagnostic_logs'
TRIGGER_SQL = f'''
CREATE TRIGGER {TRIGGER_NAME}
BEFORE INSERT ON logs
WHEN NOT (
    NEW.target = 'codex_core::session::turn'
    AND instr(COALESCE(NEW.feedback_log_body, ''), 'Turn error:') > 0
)
BEGIN
    SELECT RAISE(IGNORE);
END
'''.strip()


def codex_diagnostic_logs_enabled() -> bool:
    raw = str(os.environ.get('CCB_CODEX_DIAGNOSTIC_LOGS') or '').strip().lower()
    return raw in {'1', 'true', 'yes', 'on'}


def ensure_codex_diagnostic_log_filter(codex_home: Path, *, runtime_dir: Path | None = None) -> bool:
    """Ensure the managed Codex diagnostic-log policy is applied.

    Codex creates logs_2.sqlite lazily, so callers should treat False as "not
    settled yet" and retry later if the process is still starting or the
    database is temporarily locked.
    """
    if codex_diagnostic_logs_enabled():
        return remove_codex_diagnostic_log_filter(codex_home)
    _ensure_codex_diagnostic_log_redirect(codex_home, runtime_dir=runtime_dir)
    return install_codex_diagnostic_log_filter(codex_home)


def install_codex_diagnostic_log_filter(codex_home: Path) -> bool:
    """Drop Codex diagnostic rows from CCB-managed Codex homes.

    Returns True when the trigger is present after the call. Missing lazy-created
    Codex databases return False so long-lived callers can retry later.
    """
    db_path = Path(codex_home).expanduser() / DB_NAME
    if not db_path.is_file():
        return False
    try:
        with sqlite3.connect(str(db_path), timeout=0.2) as conn:
            if not _logs_table_exists(conn):
                return False
            current_sql = _trigger_sql(conn)
            if _trigger_sql_matches(current_sql):
                return True
            if current_sql is not None:
                conn.execute(f'DROP TRIGGER IF EXISTS {TRIGGER_NAME}')
            conn.execute(TRIGGER_SQL)
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def remove_codex_diagnostic_log_filter(codex_home: Path) -> bool:
    """Remove CCB's Codex diagnostic-log filter when diagnostics are enabled."""
    _restore_codex_diagnostic_log_redirect(codex_home)
    db_path = Path(codex_home).expanduser() / DB_NAME
    if not db_path.is_file():
        return True
    try:
        with sqlite3.connect(str(db_path), timeout=0.2) as conn:
            if _trigger_sql(conn) is None:
                return True
            conn.execute(f'DROP TRIGGER IF EXISTS {TRIGGER_NAME}')
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def ensure_codex_diagnostic_log_filter_from_env() -> bool:
    raw = str(os.environ.get('CODEX_SQLITE_HOME') or os.environ.get('CODEX_HOME') or '').strip()
    if not raw:
        return False
    runtime_dir = str(os.environ.get('CODEX_RUNTIME_DIR') or '').strip()
    return ensure_codex_diagnostic_log_filter(
        Path(raw),
        runtime_dir=Path(runtime_dir) if runtime_dir else None,
    )


def install_codex_diagnostic_log_filter_from_env() -> bool:
    raw = str(os.environ.get('CODEX_SQLITE_HOME') or os.environ.get('CODEX_HOME') or '').strip()
    if not raw:
        return False
    return install_codex_diagnostic_log_filter(Path(raw))


class CodexDiagnosticLogFilterInstaller:
    def __init__(self, interval_s: float = 5.0) -> None:
        self._ensured = False
        self._next_attempt = 0.0
        self._interval_s = max(0.1, float(interval_s))

    def maybe_install(self) -> bool:
        if self._ensured:
            return True
        now = time.monotonic()
        if now < self._next_attempt:
            return False
        self._next_attempt = now + self._interval_s
        self._ensured = ensure_codex_diagnostic_log_filter_from_env()
        return self._ensured


def _logs_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='logs' LIMIT 1"
    ).fetchone()
    return row is not None


def _ensure_codex_diagnostic_log_redirect(codex_home: Path, *, runtime_dir: Path | None = None) -> bool:
    home = Path(codex_home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / DB_NAME
    target = _diagnostic_log_temp_db_path(home, runtime_dir=runtime_dir)
    if db_path.is_symlink():
        if not _repair_existing_log_db_symlink(db_path):
            _restore_codex_diagnostic_log_redirect(home)
            return False
        return True

    for sidecar_name in (f'{DB_NAME}-wal', f'{DB_NAME}-shm'):
        sidecar = home / sidecar_name
        if sidecar.exists() or sidecar.is_symlink():
            _move_path_to_backup(sidecar)
    if db_path.exists():
        _move_path_to_backup(db_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        db_path.symlink_to(target)
    except FileExistsError:
        return db_path.is_symlink()
    except OSError:
        _restore_diagnostic_log_backups(home)
        return False
    return True


def _repair_existing_log_db_symlink(db_path: Path) -> bool:
    target = _symlink_target_path(db_path)
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if target.exists() and not target.is_file():
        return False
    _repair_preinitialized_log_db_target(target)
    return True


def _symlink_target_path(db_path: Path) -> Path | None:
    try:
        return db_path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _repair_preinitialized_log_db_target(target: Path) -> None:
    if not target.is_file():
        return
    if not _log_db_looks_preinitialized_without_migration(target):
        return
    _move_db_family_to_backup(target)


def _log_db_looks_preinitialized_without_migration(db_path: Path) -> bool:
    try:
        with sqlite3.connect(str(db_path), timeout=0.2) as conn:
            if not _logs_table_exists(conn):
                return False
            return not _codex_log_migration_one_completed(conn)
    except sqlite3.Error:
        return False


def _codex_log_migration_one_completed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_sqlx_migrations' LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM _sqlx_migrations WHERE version=1 AND success LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _move_db_family_to_backup(path: Path) -> None:
    for candidate in (path.with_name(f'{path.name}-wal'), path.with_name(f'{path.name}-shm'), path):
        if candidate.exists() or candidate.is_symlink():
            _move_path_to_backup(candidate)


def _restore_codex_diagnostic_log_redirect(codex_home: Path) -> None:
    home = Path(codex_home).expanduser()
    db_path = home / DB_NAME
    if not db_path.is_symlink():
        return
    try:
        db_path.unlink()
    except FileNotFoundError:
        pass
    _restore_diagnostic_log_backups(home)


def _restore_diagnostic_log_backups(home: Path) -> None:
    db_path = home / DB_NAME
    if not db_path.exists() and not db_path.is_symlink():
        backup = _existing_backup_path(db_path)
        if backup is not None:
            try:
                backup.rename(db_path)
            except OSError:
                pass
    for sidecar_name in (f'{DB_NAME}-wal', f'{DB_NAME}-shm'):
        sidecar = home / sidecar_name
        backup = _existing_backup_path(sidecar)
        if backup is None or sidecar.exists() or sidecar.is_symlink():
            continue
        try:
            backup.rename(sidecar)
        except OSError:
            pass


def _diagnostic_log_temp_db_path(codex_home: Path, *, runtime_dir: Path | None = None) -> Path:
    raw_root = str(os.environ.get('CCB_CODEX_LOGS_TMPDIR') or '').strip()
    if raw_root:
        root = Path(raw_root).expanduser()
    else:
        uid = getattr(os, 'getuid', lambda: 0)()
        root = Path(tempfile.gettempdir()) / f'ccb-codex-logs-{uid}'
    digest_source = str(Path(codex_home).expanduser().resolve(strict=False))
    if runtime_dir is not None:
        digest_source = f'{digest_source}\n{Path(runtime_dir).expanduser().resolve(strict=False)}'
    digest = hashlib.sha256(digest_source.encode('utf-8')).hexdigest()[:16]
    return root / digest / DB_NAME


def _move_path_to_backup(path: Path) -> Path | None:
    backup = _next_backup_path(path)
    try:
        path.rename(backup)
    except OSError:
        return None
    return backup


def _next_backup_path(path: Path) -> Path:
    base = path.with_name(f'{path.name}.bak')
    if not base.exists() and not base.is_symlink():
        return base
    for index in range(1, 1000):
        candidate = path.with_name(f'{path.name}.bak.{index}')
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    return path.with_name(f'{path.name}.bak.{int(time.time())}')


def _existing_backup_path(path: Path) -> Path | None:
    first = path.with_name(f'{path.name}.bak')
    if first.exists() or first.is_symlink():
        return first
    backups = sorted(path.parent.glob(f'{path.name}.bak.*'))
    return backups[0] if backups else None


def _trigger_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=? LIMIT 1",
        (TRIGGER_NAME,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0] or '').strip() or None


def _trigger_sql_matches(sql: str | None) -> bool:
    if not sql:
        return False
    normalized = ' '.join(sql.lower().split())
    compact = ''.join(sql.lower().split())
    return (
        f'create trigger {TRIGGER_NAME}' in normalized
        and 'before insert on logs' in normalized
        and "new.target = 'codex_core::session::turn'" in normalized
        and "'turn error:'" in normalized
        and 'raise(ignore)' in compact
    )


__all__ = [
    'CodexDiagnosticLogFilterInstaller',
    'DB_NAME',
    'TRIGGER_NAME',
    'codex_diagnostic_logs_enabled',
    'ensure_codex_diagnostic_log_filter',
    'ensure_codex_diagnostic_log_filter_from_env',
    'install_codex_diagnostic_log_filter',
    'install_codex_diagnostic_log_filter_from_env',
    'remove_codex_diagnostic_log_filter',
]
