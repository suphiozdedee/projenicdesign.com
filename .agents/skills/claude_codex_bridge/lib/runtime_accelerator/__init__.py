"""Python fallback client for the ccb-runtime-accelerator sidecar."""

from .client import AcceleratorError, call, call_or_fallback, default_socket_path
from .config import accelerator_socket_path, accelerator_timeout_s, codex_accelerator_enabled

__all__ = [
    "AcceleratorError",
    "accelerator_socket_path",
    "accelerator_timeout_s",
    "call",
    "call_or_fallback",
    "codex_accelerator_enabled",
    "default_socket_path",
]
