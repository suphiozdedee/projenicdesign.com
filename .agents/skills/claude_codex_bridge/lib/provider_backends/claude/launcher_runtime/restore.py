from __future__ import annotations

import os
from pathlib import Path

from provider_backends.session_authority import (
    current_provider_authority_fingerprint,
    linked_continuation_pending,
    provider_authority_matches,
    rebind_provider_session_authority,
    stored_provider_authority_fingerprint,
)
from provider_backends.runtime_restore import ProviderRestoreTarget, resolve_restore_context

from .history import ClaudeHistoryLocator


def resolve_claude_restore_target(
    *,
    spec,
    runtime_dir: Path,
    restore: bool,
    workspace_path: Path | None = None,
    project_session_restore_target_fn,
    claude_history_state_fn,
    claude_home_layout_fn,
    load_profile_fn,
) -> ProviderRestoreTarget:
    context = resolve_restore_context(
        runtime_dir,
        provider='claude',
        agent_name=spec.name,
        workspace_path=workspace_path,
    )
    default_target = ProviderRestoreTarget(run_cwd=context.workspace_path, has_history=False)
    if not restore:
        return default_target

    profile = load_profile_fn(runtime_dir)
    authority_fingerprint = current_provider_authority_fingerprint('claude', profile, runtime_dir)
    home_layout = claude_home_layout_fn(runtime_dir, profile)
    session_target = project_session_restore_target_fn(
        context.workspace_path,
        context.session_instance,
        managed_home=home_layout.home_root,
        authority_fingerprint=authority_fingerprint,
    )
    if session_target is not None:
        return session_target

    managed_workspace = is_ccb_managed_workspace(context.workspace_path)
    project_root = context.workspace_path if managed_workspace else (context.project_root or context.workspace_path)
    _session_id, has_history, best_cwd = claude_history_state_fn(
        invocation_dir=context.workspace_path,
        project_root=project_root,
        include_env_pwd=not managed_workspace,
        home_dir=home_layout.home_root,
    )
    if has_history:
        return ProviderRestoreTarget(run_cwd=existing_dir(best_cwd) or context.workspace_path, has_history=True)
    return default_target


def project_session_restore_target(
    workspace_path: Path,
    session_instance: str | None,
    *,
    load_project_session_fn,
    claude_history_state_fn,
    managed_home: Path,
    authority_fingerprint: str,
) -> ProviderRestoreTarget | None:
    session = load_project_session_fn(workspace_path, instance=session_instance)
    if session is None:
        return None
    data = getattr(session, 'data', {}) or {}
    authority_matches = provider_authority_matches(
        data,
        'claude',
        authority_fingerprint,
        allow_legacy_missing=True,
    )
    session_cwd = existing_dir(getattr(session, 'work_dir', ''))
    stored_fingerprint = stored_provider_authority_fingerprint(data, 'claude')
    if stored_fingerprint and not authority_matches:
        # A known authority change must never inspect or resume native history.
        # Missing legacy metadata is handled below as adoptable evidence.
        rebind_provider_session_authority(
            session,
            'claude',
            authority_fingerprint,
            native_resume_compatible=False,
        )
        continuation_id = linked_continuation_session_id(
            data,
            managed_home=managed_home,
        )
        return ProviderRestoreTarget(
            run_cwd=session_cwd or workspace_path,
            has_history=False,
            continuation_session_id=continuation_id,
            continuation_mode='fork' if continuation_id else None,
        )
    if linked_continuation_pending(data, 'claude'):
        continuation_id = linked_continuation_session_id(
            data,
            managed_home=managed_home,
        )
        return ProviderRestoreTarget(
            run_cwd=session_cwd or workspace_path,
            has_history=False,
            continuation_session_id=continuation_id,
            continuation_mode='fork' if continuation_id else None,
        )
    if session_cwd is None:
        if not authority_matches:
            rebind_provider_session_authority(
                session,
                'claude',
                authority_fingerprint,
                native_resume_compatible=False,
            )
        return None
    session_home = getattr(session, 'claude_home_path', None)
    if session_home is None or not _is_within_root(session_home, managed_home):
        # Keep the CCB binding as recoverable linked history, but never resume
        # a path outside the current Agent-owned home.
        if not authority_matches or _has_native_binding(data, 'claude'):
            rebind_provider_session_authority(
                session,
                'claude',
                authority_fingerprint,
                native_resume_compatible=False,
            )
        return ProviderRestoreTarget(run_cwd=session_cwd, has_history=False)
    _session_id, has_history, best_cwd = claude_history_state_fn(
        invocation_dir=session_cwd,
        project_root=session_cwd,
        include_env_pwd=False,
        home_dir=session_home,
    )
    if not has_history:
        if not authority_matches:
            rebind_provider_session_authority(
                session,
                'claude',
                authority_fingerprint,
                native_resume_compatible=False,
            )
        return None
    native_compatible = _native_history_binding_is_safe(data, 'claude', session_home)
    rebind_provider_session_authority(
        session,
        'claude',
        authority_fingerprint,
        native_resume_compatible=native_compatible,
    )
    if not native_compatible:
        return ProviderRestoreTarget(run_cwd=existing_dir(best_cwd) or session_cwd, has_history=False)
    return ProviderRestoreTarget(run_cwd=existing_dir(best_cwd) or session_cwd, has_history=True)


def claude_history_state(
    *,
    invocation_dir: Path,
    project_root: Path,
    env: dict[str, str] | None = None,
    home_dir: Path,
) -> tuple[str | None, bool, Path | None]:
    home = home_dir if home_dir is not None else Path.home()
    locator = ClaudeHistoryLocator(
        invocation_dir=invocation_dir,
        project_root=project_root,
        env=env or {},
        home_dir=home,
    )
    return locator.latest_session_id()


def existing_dir(value: object) -> Path | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        path = Path(raw).expanduser()
    except Exception:
        return None
    return path if path.is_dir() else None


def is_ccb_managed_workspace(workspace_path: Path) -> bool:
    try:
        return (workspace_path / ".ccb-workspace.json").is_file()
    except Exception:
        return False


def _is_within_root(candidate: Path, managed_root: Path) -> bool:
    normalized_candidate = _normalize_path(candidate)
    normalized_managed = _normalize_path(managed_root)
    if normalized_candidate is None or normalized_managed is None:
        return False
    try:
        normalized_candidate.relative_to(normalized_managed)
        return True
    except Exception:
        return False


def _normalize_path(value: object) -> Path | None:
    try:
        return Path(value).expanduser().resolve()
    except Exception:
        try:
            return Path(value).expanduser()
        except Exception:
            return None


def _has_native_binding(data: object, provider: str) -> bool:
    if not isinstance(data, dict):
        return False
    return bool(
        str(data.get(f'{provider}_session_id') or '').strip()
        or str(data.get(f'{provider}_session_path') or '').strip()
    )


def _native_history_binding_is_safe(data: object, provider: str, managed_home: Path) -> bool:
    if not isinstance(data, dict):
        return True
    raw_path = str(data.get(f'{provider}_session_path') or '').strip()
    if not raw_path:
        return True
    try:
        candidate = Path(raw_path).expanduser().resolve()
        root = Path(managed_home).expanduser().resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return False
    return candidate.is_file()


def linked_continuation_session_id(
    data: dict[str, object],
    *,
    managed_home: Path,
) -> str | None:
    session_id = str(
        data.get('old_claude_session_id')
        or data.get('claude_session_id')
        or ''
    ).strip()
    raw_path = str(
        data.get('old_claude_session_path')
        or data.get('claude_session_path')
        or ''
    ).strip()
    if not session_id or not raw_path:
        return None
    try:
        session_path = Path(raw_path).expanduser().resolve()
        root = Path(managed_home).expanduser().resolve()
        session_path.relative_to(root)
    except (OSError, ValueError):
        return None
    return session_id if session_path.is_file() else None


__all__ = [
    'claude_history_state',
    'existing_dir',
    'is_ccb_managed_workspace',
    'linked_continuation_session_id',
    'project_session_restore_target',
    'resolve_claude_restore_target',
]
