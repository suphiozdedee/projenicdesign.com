from __future__ import annotations

import os
from pathlib import Path

from opencode_runtime.paths import env_truthy
from opencode_runtime.storage import OpenCodeStorageAccessor
from provider_backends.opencode.runtime.log_reader_facade_runtime.cancel import OpenCodeCancelMixin
from provider_backends.opencode.runtime.log_reader_facade_runtime.config import (
    bounded_force_read_interval,
    bounded_poll_interval,
)
from provider_backends.opencode.runtime.log_reader_facade_runtime.state import apply_project_scope
from provider_backends.opencode.runtime.log_reader_facade_runtime.storage import OpenCodeStorageMixin
from provider_backends.opencode.runtime.log_reader_facade_runtime.timeline import OpenCodeTimelineMixin


class MimoLogReader(OpenCodeStorageMixin, OpenCodeTimelineMixin, OpenCodeCancelMixin):
    def __init__(
        self,
        root=None,
        work_dir=None,
        project_id: str = "global",
        *,
        session_id_filter: str | None = None,
    ):
        self.root = Path(root or default_mimo_storage_root()).expanduser()
        self._storage = OpenCodeStorageAccessor(
            self.root,
            db_env_var="MIMOCODE_DB",
            db_filenames=("mimocode.db",),
        )
        self.work_dir = work_dir or Path.cwd()
        env_project_id = (os.environ.get("MIMOCODE_PROJECT_ID") or "").strip()
        explicit_project_id = bool(env_project_id) or ((project_id or "").strip() not in ("", "global"))
        self._allow_parent_match = env_truthy("MIMOCODE_ALLOW_PARENT_WORKDIR_MATCH")
        self._allow_any_session = env_truthy("MIMOCODE_ALLOW_ANY_SESSION")
        self._allow_session_rollover = env_truthy("MIMOCODE_ALLOW_SESSION_ROLLOVER")
        self.project_id = (env_project_id or project_id or "global").strip() or "global"
        self._session_id_filter = (session_id_filter or "").strip() or None
        apply_project_scope(
            self,
            explicit_project_id=explicit_project_id,
            allow_git_root_fallback=env_truthy("MIMOCODE_ALLOW_GIT_ROOT_FALLBACK"),
        )
        self._poll_interval = bounded_poll_interval()
        self._force_read_interval = bounded_force_read_interval()


def default_mimo_storage_root(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    home = str(env.get("MIMOCODE_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "data" / "storage"
    xdg_data_home = str(env.get("XDG_DATA_HOME") or "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "mimocode" / "storage"
    return Path.home() / ".local" / "share" / "mimocode" / "storage"


__all__ = ["MimoLogReader", "default_mimo_storage_root"]
