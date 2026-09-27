from __future__ import annotations


def listen_server(server) -> None:
    if server._server is not None:
        return
    listener = None
    bound_socket_stat = None
    try:
        listener = server._control_plane_transport.listen()
        bound_socket_stat = getattr(listener, 'bound_socket_stat', None)
    except BaseException:
        try:
            if listener is not None:
                listener.close()
        finally:
            if bound_socket_stat is not None:
                server._control_plane_transport.unlink_bound_endpoint(bound_identity=bound_socket_stat)
        raise
    server._reset_worker_error()
    server._server = listener
    endpoint = getattr(listener, 'endpoint', None)
    if isinstance(endpoint, dict):
        server._control_plane_endpoint = dict(endpoint)
    server._bound_socket_stat = bound_socket_stat
    server._stop_event.clear()


def shutdown_server(server) -> None:
    server._stop_event.set()
    bound_socket_stat = server._bound_socket_stat
    if server._server is not None:
        try:
            server._server.close()
        finally:
            server._server = None
    server._control_plane_transport.unlink_bound_endpoint(bound_identity=bound_socket_stat)
    server._bound_socket_stat = None
    server._bootstrap_probe_active = False
    server._runtime_bootstrap_active = False


__all__ = ['listen_server', 'shutdown_server']
