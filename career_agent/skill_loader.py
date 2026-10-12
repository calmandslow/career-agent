from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SAFE_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    name: str
    instructions: str


class SkillNotFoundError(FileNotFoundError):
    pass


class SkillLoader:
    def __init__(self, root: str | Path = "skills") -> None:
        self.root = Path(root)

    def load(self, name: str) -> LoadedSkill:
        if not _SAFE_SKILL_NAME.fullmatch(name):
            raise ValueError("Invalid skill name")
        path = self.root / name / "SKILL.md"
        if not path.is_file():
            raise SkillNotFoundError(name)
        instructions = path.read_text(encoding="utf-8").strip()
        if not instructions:
            raise ValueError(f"Skill is empty: {name}")
        return LoadedSkill(name=name, instructions=instructions)
