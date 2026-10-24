from __future__ import annotations

import os
from dataclasses import dataclass, field


FAST_SKILLS = frozenset({
    "material-ocr",
    "jd-analyzer",
    "interview-research",
    "resume-claim-verifier",
})

CORE_SKILLS = frozenset({
    "career-agent",
    "intent-router",
    "resume-tailor",
    "interview-question-generator",
    "interview-answer-coach",
    "mock-interview",
    "interview-review",
})


@dataclass(frozen=True, slots=True)
class ModelRouter:
    """Maps each skill to a cost/capability tier without changing the Planner."""

    fast_model: str
    core_model: str
    overrides: dict[str, str] = field(default_factory=dict)

    def model_for(self, skill_name: str) -> str:
        if skill_name in self.overrides:
            return self.overrides[skill_name]
        if skill_name in FAST_SKILLS:
            return self.fast_model
        if skill_name in CORE_SKILLS:
            return self.core_model
        raise ValueError(f"No model route registered for skill: {skill_name}")

    @classmethod
    def from_qwen_env(cls) -> "ModelRouter":
        # QWEN_MODEL remains a backward-compatible global override. If it is
        # present, both tiers use it unless a tier-specific variable is set.
        legacy_model = os.getenv("QWEN_MODEL")
        return cls(
            overrides={"answer-semantic-verifier": os.getenv("QWEN_REVIEW_MODEL") or os.getenv("QWEN_CORE_MODEL") or legacy_model or "qwen3.7-plus"},
            fast_model=os.getenv("QWEN_FAST_MODEL") or legacy_model or "qwen3.8-flash",
            core_model=os.getenv("QWEN_CORE_MODEL") or legacy_model or "qwen3.7-plus",
        )
