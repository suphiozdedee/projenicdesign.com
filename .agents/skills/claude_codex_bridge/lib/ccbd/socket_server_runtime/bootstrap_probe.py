from __future__ import annotations


def bootstrap_readiness_probe(server, *, timeout_s: float):
    """Prove the control-plane endpoint and normal request worker before mounted publish."""

    return server._control_plane_transport.bootstrap_readiness_probe(
        server,
        timeout_s=timeout_s,
    )


__all__ = ['bootstrap_readiness_probe']
