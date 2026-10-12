from __future__ import annotations

import os

from .executor import OpenAIResponsesExecutor, QwenExecutor, SkillExecutor
from .mock_executor import MockSkillExecutor
from .telemetry import ProgressReporter


def create_executor(
    provider: str | None = None,
    *,
    progress: ProgressReporter | None = None,
) -> SkillExecutor:
    selected = (provider or os.getenv("LLM_PROVIDER", "mock")).lower()
    if selected == "mock":
        return MockSkillExecutor()
    if selected == "openai":
        return OpenAIResponsesExecutor(progress=progress)
    if selected in {"qwen", "dashscope"}:
        return QwenExecutor(progress=progress)
    raise ValueError(f"Unsupported LLM_PROVIDER: {selected}")
