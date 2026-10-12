from __future__ import annotations

import json
import re
from pathlib import Path

from .models import ApplicationState


_SAFE_ID = re.compile(r"^job_[a-f0-9]{12}$")


class StateNotFoundError(FileNotFoundError):
    pass


class JsonStateRepository:
    def __init__(self, directory: str | Path = "data/applications") -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, application_id: str) -> Path:
        if not _SAFE_ID.fullmatch(application_id):
            raise ValueError("Invalid application_id")
        return self.directory / f"{application_id}.json"

    def save(self, state: ApplicationState) -> None:
        path = self._path(state.application_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def load(self, application_id: str) -> ApplicationState:
        path = self._path(application_id)
        if not path.exists():
            raise StateNotFoundError(application_id)
        return ApplicationState.from_dict(json.loads(path.read_text(encoding="utf-8")))
