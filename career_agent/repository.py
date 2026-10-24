from __future__ import annotations

import json
import re
import time
import threading
from pathlib import Path
from uuid import uuid4

from .models import ApplicationState


_SAFE_ID = re.compile(r"^job_[a-f0-9]{12}$")


class StateNotFoundError(FileNotFoundError):
    pass


class JsonStateRepository:
    def __init__(self, directory: str | Path = "data/applications") -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, application_id: str) -> Path:
        if not _SAFE_ID.fullmatch(application_id):
            raise ValueError("Invalid application_id")
        return self.directory / f"{application_id}.json"

    def save(self, state: ApplicationState) -> None:
        with self._lock:
            path = self._path(state.application_id)
            # A unique receipt prevents the web poller and a worker from contending
            # for the same .tmp filename on Windows while preserving atomic replace.
            temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
            temporary.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            for attempt in range(4):
                try:
                    temporary.replace(path)
                    break
                except PermissionError:
                    if attempt == 3:
                        # Some Windows app sandboxes allow ordinary writes in their
                        # temp root but deny ReplaceFile/os.replace. Keep the lock
                        # and use a direct-write fallback only in that environment.
                        path.write_bytes(temporary.read_bytes())
                        temporary.unlink(missing_ok=True)
                        break
                    time.sleep(0.01 * (attempt + 1))

    def load(self, application_id: str) -> ApplicationState:
        with self._lock:
            path = self._path(application_id)
            if not path.exists():
                raise StateNotFoundError(application_id)
            for attempt in range(4):
                try:
                    return ApplicationState.from_dict(json.loads(path.read_text(encoding="utf-8")))
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        raise RuntimeError("unreachable")
