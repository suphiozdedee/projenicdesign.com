from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    'current_service_graph': ('.service_graph', 'current_ccbd_service_graph'),
    'execute_project_stop': ('.lifecycle', 'execute_project_stop'),
    'finalize_project_stop': ('.lifecycle', 'finalize_project_stop'),
    'heartbeat': ('.lifecycle', 'heartbeat'),
    'initialize_app': ('.bootstrap', 'initialize_app'),
    'mount_agent_from_policy': ('.policy', 'mount_agent_from_policy'),
    'persist_start_policy': ('.policy', 'persist_start_policy'),
    'publish_service_graph': ('.service_graph', 'publish_ccbd_service_graph'),
    'prepare_project_stop': ('.lifecycle', 'prepare_project_stop'),
    'record_shutdown_report': ('.lifecycle', 'record_shutdown_report'),
    'record_startup_report': ('.lifecycle', 'record_startup_report'),
    'recovery_start_options': ('.policy', 'recovery_start_options'),
    'register_handlers': ('.handlers', 'register_handlers'),
    'release_backend_ownership': ('.lifecycle', 'release_backend_ownership'),
    'remount_project_from_policy': ('.policy', 'remount_project_from_policy'),
    'request_shutdown': ('.lifecycle', 'request_shutdown'),
    'serve_forever': ('.lifecycle', 'serve_forever'),
    'shutdown': ('.lifecycle', 'shutdown'),
    'start': ('.lifecycle', 'start'),
}


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}') from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_EXPORTS))


__all__ = list(_EXPORTS)
