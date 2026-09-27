from __future__ import annotations

from pathlib import Path
import os
import platform

from agents.config_loader import load_project_config
from ccbd.socket_client import CcbdClient
from provider_core.catalog import build_default_provider_catalog
from provider_execution.registry import build_default_execution_registry
from platforms.windows.herdr.supportability_projection import load_matrix as load_herdr_support_matrix
from platforms.windows.release.workflow_matrix import MATRIX_RELATIVE_PATH as HERDR_MATRIX_PATH
from platforms.windows.release.surface import load_windows_x64_release_surface_projection

from .daemon import ping_local_state
from .daemon_runtime.policy import CONTROL_PLANE_RPC_TIMEOUT_S
from .config_validate import validate_config_context
from .doctor_runtime import (
    agent_summaries,
    ccbd_summary,
    doctor_stores,
    entrypoint_summary,
    installation_summary,
    requirements_summary,
    runtime_identity_summary,
)


def doctor_summary(context) -> dict:
    config = load_project_config(context.project.project_root).config
    config_validation = validate_config_context(context)
    stores = doctor_stores(context)
    installation = installation_summary()
    catalog = build_default_provider_catalog()
    execution_registry = build_default_execution_registry()
    local = ping_local_state(context)
    errors: list[str] = []
    remote_client, remote_client_error = _load_remote_client(context, local=local)
    remote_ccbd, remote_error = _load_remote_ccbd_summary(
        context,
        local=local,
        client=remote_client,
        client_error=remote_client_error,
    )
    if remote_error is not None:
        errors.append(f'remote_ccbd_probe:{remote_error}')
    active_inbound_diagnostics, project_view_error = _load_remote_project_view_diagnostics(
        context,
        local=local,
        client=remote_client,
        client_error=remote_client_error,
    )
    if project_view_error is not None:
        errors.append(f'remote_project_view_probe:{project_view_error}')
    agents = agent_summaries(
        context,
        config=config,
        stores=stores,
        catalog=catalog,
        execution_registry=execution_registry,
        errors=errors,
    )
    return {
        'project': str(context.project.project_root),
        'project_id': context.project.project_id,
        'installation': installation,
        'entrypoint': entrypoint_summary(installation=installation),
        'windows_x64_release_surface': _windows_x64_release_surface_summary(installation),
        'herdr': _herdr_supportability_summary(context),
        'runtime': runtime_identity_summary(
            context.project.project_root,
            ccb_dir=context.paths.ccb_dir,
            installation=installation,
        ),
        'requirements': requirements_summary(),
        'config': config_validation.to_record(),
        'ccbd': ccbd_summary(local=local, stores=stores, errors=errors, remote=remote_ccbd),
        'active_inbound_diagnostics': active_inbound_diagnostics,
        'agents': agents,
    }


def _windows_x64_release_surface_summary(installation: dict[str, object]) -> dict[str, object]:
    root_text = str(installation.get('path') or '').strip()
    root = Path(root_text).expanduser() if root_text else Path.cwd()
    return load_windows_x64_release_surface_projection(
        root,
        _windows_x64_release_surface_host_evidence(),
    )


def _windows_x64_release_surface_host_evidence() -> dict[str, object]:
    env_arch = str(os.environ.get('PROCESSOR_ARCHITECTURE') or platform.machine() or '').strip()
    native_arch = str(os.environ.get('PROCESSOR_ARCHITEW6432') or '').strip()
    return {
        'os_platform': _release_surface_os_platform(platform.system()),
        'cpu_arch': _release_surface_cpu_arch(native_arch or env_arch or platform.machine()),
        'process_arch': _release_surface_cpu_arch(env_arch or platform.machine()),
        'wow64': bool(native_arch and _release_surface_cpu_arch(env_arch) == 'ia32'),
        'python_executable': None,
        'python_bitness': 'unknown',
        'installer_entrypoint': 'doctor',
    }


def _herdr_supportability_summary(context) -> dict[str, object]:
    """Compute the Herdr support tier from the validation matrix artifact.

    When the matrix is unavailable or unreadable, returns a projection with
    ``support_tier="unsupported"`` and ``support_tier_source="missing"`` so
    that ``ccb doctor --output`` always includes a ``herdr`` key.
    """
    try:
        repo_root = Path(str(getattr(context, 'project', None) and getattr(context.project, 'project_root', None) or ''))
        if not repo_root.is_dir():
            repo_root = Path.cwd()
    except Exception:
        repo_root = Path.cwd()
    matrix_path = repo_root / HERDR_MATRIX_PATH
    projection = load_herdr_support_matrix(str(matrix_path), repo_root=str(repo_root))
    return {
        'support_tier': projection.get('support_tier', 'unsupported'),
        'support_tier_source': projection.get('support_tier_source', 'missing'),
        'herdr_version': projection.get('herdr_version'),
        'herdr_auto_restore_mode': projection.get('herdr_auto_restore_mode', 'unknown'),
        'required_workflows_status': projection.get('required_workflows_status', 'missing'),
        'provider_workflows_status': projection.get('provider_workflows_status', 'missing'),
        'mobile_terminal_status': projection.get('mobile_terminal_status', 'not-run'),
        'config_ui_status': projection.get('config_ui_status', 'not-run'),
        'non_pass_workflows': projection.get('non_pass_workflows', {}),
        'fallback_guidance': projection.get('fallback_guidance', ''),
        'beta_gaps': projection.get('beta_gaps', []),
        'residual_risks': projection.get('residual_risks', []),
    }


def _release_surface_os_platform(system_name: str) -> str:
    if system_name == 'Windows':
        return 'win32'
    if system_name == 'Darwin':
        return 'darwin'
    if system_name == 'Linux':
        return 'linux'
    return 'unknown'


def _release_surface_cpu_arch(value: str) -> str:
    normalized = str(value or '').strip().lower()
    if normalized in {'amd64', 'x86_64', 'x64'}:
        return 'x64'
    if normalized in {'aarch64', 'arm64'}:
        return 'arm64'
    if normalized in {'x86', 'i386', 'i686', 'ia32'}:
        return 'ia32'
    return 'unknown'


def _load_remote_client(context, *, local) -> tuple[object | None, str | None]:
    if local.mount_state == 'unmounted':
        return None, None
    if not local.socket_connectable:
        return None, None
    try:
        return CcbdClient(context.paths.ccbd_socket_path, timeout_s=CONTROL_PLANE_RPC_TIMEOUT_S), None
    except Exception as exc:
        return None, str(exc)


def _load_remote_ccbd_summary(
    context,
    *,
    local,
    client: object | None = None,
    client_error: str | None = None,
) -> tuple[dict | None, str | None]:
    if local.mount_state == 'unmounted' or not local.socket_connectable:
        return None, None
    if client_error is not None:
        return None, client_error
    if client is None:
        client, client_error = _load_remote_client(context, local=local)
        if client_error is not None or client is None:
            return None, client_error
    try:
        payload = client.ping('ccbd')
    except Exception as exc:
        return None, str(exc)
    return (payload if isinstance(payload, dict) else None), None


def _load_remote_project_view_diagnostics(
    context,
    *,
    local,
    client: object | None = None,
    client_error: str | None = None,
) -> tuple[list[dict[str, object]], str | None]:
    if local.mount_state == 'unmounted' or not local.socket_connectable:
        return [], None
    if client_error is not None:
        return [], client_error
    try:
        if client is None:
            client, client_error = _load_remote_client(context, local=local)
            if client_error is not None or client is None:
                return [], client_error
        project_view = getattr(client, 'project_view', None)
        if not callable(project_view):
            return [], None
        payload = project_view(schema_version=1)
    except Exception as exc:
        return [], str(exc)
    return _active_inbound_diagnostics_from_project_view(payload), None


def _active_inbound_diagnostics_from_project_view(payload: object) -> list[dict[str, object]]:
    view = payload.get('view') if isinstance(payload, dict) else None
    comms = view.get('comms') if isinstance(view, dict) else None
    result: list[dict[str, object]] = []
    for comm in comms or ():
        if not isinstance(comm, dict):
            continue
        diagnostic = comm.get('active_inbound_diagnostic')
        if not isinstance(diagnostic, dict):
            continue
        comm_job_id = str(comm.get('id') or '').strip()
        diagnostic_job_id = str(diagnostic.get('job_id') or '').strip()
        if (
            comm_job_id
            and diagnostic_job_id == comm_job_id
            and str(diagnostic.get('condition_kind') or '').strip() == 'orphaned_active_inbound'
        ):
            result.append(dict(diagnostic))
    return result
