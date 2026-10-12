from __future__ import annotations

import json
from pathlib import Path

from career_agent.executor_factory import create_executor
from career_agent.schemas import JDProfile
from career_agent.skill_loader import SkillLoader
from career_agent.telemetry import ProgressReporter


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    skill = SkillLoader(PROJECT_ROOT / "skills").load("jd-analyzer")
    executor = create_executor("qwen", progress=ProgressReporter(enabled=True))
    result = executor.execute(
        skill,
        {
            "company": "示例 AI 公司",
            "jd": (
                "负责 AI Agent 产品规划，分析用户需求并设计产品方案；"
                "与算法、研发和设计团队协作推动上线；"
                "建立效果评估指标并持续迭代。"
                "要求具备 AI 产品或大模型应用经验、良好的数据分析和跨团队协作能力。"
            ),
        },
        JDProfile,
    )
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
