from __future__ import annotations

from time import monotonic

from agents.config_loader import load_project_config, project_config_path
from ccbd.reload_apply import run_additive_reload_apply
from ccbd.reload_drain_status import reload_drain_status_payload
from ccbd.reload_plan import build_invalid_reload_dry_run_plan, build_reload_dry_run_plan
from project_command_trust import require_project_command_approval
from .project_reload_metrics import metrics_fields
from .project_reload_payload import (
    apply_reload_payload,
    non_dry_run_invalid_config_payload,
)


def build_project_reload_config_handler(app, current_graph_fn):
    def handle(payload: dict) -> dict:
        dry_run = _truthy(payload.get('dry_run'))
        started = monotonic()
        plan_class = 'error'
        error_text = None
        try:
            graph = current_graph_fn()
            try:
                config_path = project_config_path(app.project_root)
                if not config_path.is_file():
                    raise FileNotFoundError(f'project config not found: {config_path}')
                new_config = load_project_config(app.project_root).config
                require_project_command_approval(app.project_root)
            except Exception as exc:
                plan = build_invalid_reload_dry_run_plan(
                    graph.config,
                    exc,
                    current_config_identity=graph.config_identity,
                )
                if not dry_run:
                    plan = non_dry_run_invalid_config_payload(plan)
            else:
                if dry_run:
                    plan = build_reload_dry_run_plan(
                        graph.config,
                        new_config,
                        current_config_identity=graph.config_identity,
                        project_id=getattr(app, 'project_id', None),
                        current_namespace=_current_namespace(app),
                    )
                else:
                    plan = apply_reload_payload(run_additive_reload_apply(app, new_config), app=app)
            payload = _with_reload_drains(app, plan)
            plan_class, error_text = metrics_fields(payload, fallback_plan_class=plan_class)
            return payload
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            metrics = getattr(app, 'control_plane_metrics', None)
            if metrics is not None:
                metrics.last_reload_duration_s = max(0.0, monotonic() - started)
                metrics.last_reload_plan_class = plan_class
                metrics.last_reload_error = error_text

    return handle


def _truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _current_namespace(app):
    namespace_controller = getattr(app, 'project_namespace', None)
    load = getattr(namespace_controller, 'load', None)
    if not callable(load):
        return None
    try:
        return load()
    except Exception:
        return None


def _with_reload_drains(app, payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result['reload_drains'] = reload_drain_status_payload(app)
    return result


__all__ = ['build_project_reload_config_handler']
