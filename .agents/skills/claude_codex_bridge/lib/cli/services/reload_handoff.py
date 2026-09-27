from __future__ import annotations

from agents.config_identity import project_config_identity_payload
from agents.config_loader import load_project_config
from ccbd.reload_handoff import ReloadHandoff, ReloadHandoffStore
from ccbd.services.mount import MountManager
from ccbd.system import utc_now
from cli.context import CliContext


def begin_cli_reload_handoff(context: CliContext) -> bool:
    try:
        handoff = _build_handoff(context)
    except Exception:
        return False
    if handoff is None:
        return False
    ReloadHandoffStore(context.paths).save(handoff)
    return True


def clear_cli_reload_handoff(context: CliContext) -> None:
    ReloadHandoffStore(context.paths).clear()


def _build_handoff(context: CliContext) -> ReloadHandoff | None:
    if not context.paths.ccbd_lease_path.exists():
        return None
    lease = MountManager(context.paths).load_state()
    if lease is None:
        return None
    old_signature = _text(getattr(lease, 'config_signature', None))
    target_signature = _target_signature(context)
    daemon_instance_id = _text(getattr(lease, 'daemon_instance_id', None))
    if not old_signature or not target_signature or old_signature == target_signature:
        return None
    if not daemon_instance_id:
        return None
    return ReloadHandoff(
        project_id=context.project.project_id,
        started_at=utc_now(),
        old_config_signature=old_signature,
        target_config_signature=target_signature,
        daemon_pid=int(getattr(lease, 'ccbd_pid', 0) or 0),
        daemon_instance_id=daemon_instance_id,
        generation=int(getattr(lease, 'generation', 0) or 0),
    )


def _target_signature(context: CliContext) -> str:
    config = load_project_config(context.project.project_root).config
    return _text(project_config_identity_payload(config).get('config_signature'))


def _text(value: object) -> str:
    return str(value or '').strip()


__all__ = ['begin_cli_reload_handoff', 'clear_cli_reload_handoff']
