from __future__ import annotations

import json
import os
import base64
import mimetypes
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from .model_router import ModelRouter
from .answer_policy import answer_policy_instructions
from .skill_loader import LoadedSkill
from .telemetry import ObservableCalls, ProgressReporter


OutputT = TypeVar("OutputT", bound=BaseModel)


class SkillExecutor(Protocol):
    def execute(
        self,
        skill: LoadedSkill,
        context: dict[str, Any],
        output_model: type[OutputT],
    ) -> OutputT: ...


class OpenAIResponsesExecutor(ObservableCalls):
    """Executes one skill with the OpenAI Responses API and Structured Outputs."""

    def __init__(
        self,
        *,
        model: str | None = None,
        client: Any | None = None,
        progress: ProgressReporter | None = None,
    ) -> None:
        super().__init__(progress=progress)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-6-astra")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("Install dependencies with: pip install -r requirements.txt") from exc
            client = OpenAI(
                timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
                max_retries=0,
            )
        self.client = client

    def execute(
        self,
        skill: LoadedSkill,
        context: dict[str, Any],
        output_model: type[OutputT],
    ) -> OutputT:
        response = self._call_with_retry(
            lambda: self.client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You execute exactly one Career Agent skill. Follow its instructions, "
                            "use only the supplied context, and never invent candidate evidence.\n\n"
                            f"SKILL:\n{skill.instructions}{answer_policy_instructions(skill.name)}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": "CURRENT CONTEXT:\n" + json.dumps(context, ensure_ascii=False),
                    },
                ],
                text_format=output_model,
            ),
            skill_name=skill.name,
            call_type="skill",
            model=self.model,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("The model returned no parsed output")
        return parsed


class OpenAICompatibleChatExecutor(ObservableCalls):
    """Executes skills through an OpenAI-compatible Chat Completions API."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        client: Any | None = None,
        model_router: ModelRouter | None = None,
        progress: ProgressReporter | None = None,
    ) -> None:
        super().__init__(progress=progress)
        if not api_key:
            raise RuntimeError("Missing API key for the selected LLM provider")
        self.model = model
        self.model_router = model_router
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("Install dependencies with: pip install -r requirements.txt") from exc
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
                max_retries=0,
            )
        self.client = client

    def execute(
        self,
        skill: LoadedSkill,
        context: dict[str, Any],
        output_model: type[OutputT],
    ) -> OutputT:
        model = self.model_for_skill(skill.name)
        response = self._call_with_retry(
            lambda: self.client.chat.completions.parse(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You execute exactly one Career Agent skill. Follow its instructions, "
                            "use only the supplied context, never invent candidate evidence, and "
                            "return data matching the requested schema.\n\n"
                            f"SKILL:\n{skill.instructions}{answer_policy_instructions(skill.name)}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": "CURRENT CONTEXT:\n" + json.dumps(context, ensure_ascii=False),
                    },
                ],
                response_format=output_model,
                temperature=0.1,
            ),
            skill_name=skill.name,
            call_type=(
                "semantic_review"
                if skill.name in {"resume-claim-verifier", "answer-semantic-verifier"}
                else "skill"
            ),
            model=model,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("The model returned no parsed output")
        return parsed

    def model_for_skill(self, skill_name: str) -> str:
        if self.model_router is not None:
            return self.model_router.model_for(skill_name)
        return self.model

    def extract_image_text(self, image_path: str | Path) -> str:
        """Extract faithful text from a local screenshot with a multimodal model."""
        path = Path(image_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        model = self.model_for_skill("material-ocr")
        response = self._call_with_retry(
            lambda: self.client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "请逐字提取这张招聘岗位截图中的可见文字。保持原有顺序和分段；"
                                    "不要总结、补全、改写或推测截图外的内容；只输出提取后的纯文本。"
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                            },
                        ],
                    }
                ],
                temperature=0,
            ),
            skill_name="material-ocr",
            call_type="ocr",
            model=model,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError(f"No text was extracted from image: {path.name}")
        return content.strip()

class QwenExecutor(OpenAICompatibleChatExecutor):
    """Qwen adapter for Alibaba Cloud Model Studio's OpenAI-compatible API."""

    answer_semantic_enabled = True

    def __init__(
        self,
        *,
        client: Any | None = None,
        progress: ProgressReporter | None = None,
    ) -> None:
        router = ModelRouter.from_qwen_env()
        super().__init__(
            model=router.fast_model,
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            base_url=os.getenv(
                "DASHSCOPE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            client=client,
            model_router=router,
            progress=progress,
        )
