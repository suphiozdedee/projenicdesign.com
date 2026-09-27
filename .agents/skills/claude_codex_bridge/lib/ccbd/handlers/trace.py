from __future__ import annotations


def build_trace_handler(dispatcher, *, project_view_service=None):
    def handle(payload: dict) -> dict:
        target = str(payload.get('target') or '').strip()
        if not target:
            raise ValueError('trace requires target')
        result = dict(dispatcher.trace(target))
        diagnostics = _matching_active_inbound_diagnostics(
            result,
            project_view_service=project_view_service,
        )
        if diagnostics:
            result['active_inbound_diagnostics'] = diagnostics
        return result

    return handle


def _matching_active_inbound_diagnostics(
    trace: dict[str, object],
    *,
    project_view_service,
) -> list[dict[str, object]]:
    if project_view_service is None:
        return []
    job_ids = {str(trace.get('job_id') or '').strip()}
    for job in trace.get('jobs') or ():
        if isinstance(job, dict):
            job_ids.add(str(job.get('job_id') or '').strip())
    job_ids.discard('')
    if not job_ids:
        return []
    try:
        response = project_view_service.build_response(schema_version=1)
    except Exception:
        return []
    view = response.get('view') if isinstance(response, dict) else None
    comms = view.get('comms') if isinstance(view, dict) else None
    result: list[dict[str, object]] = []
    for comm in comms or ():
        if not isinstance(comm, dict) or str(comm.get('id') or '').strip() not in job_ids:
            continue
        diagnostic = comm.get('active_inbound_diagnostic')
        if not isinstance(diagnostic, dict):
            continue
        job_id = str(diagnostic.get('job_id') or '').strip()
        if (
            job_id in job_ids
            and str(diagnostic.get('condition_kind') or '').strip() == 'orphaned_active_inbound'
        ):
            result.append(dict(diagnostic))
    return result
