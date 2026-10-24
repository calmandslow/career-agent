"""Run a small, isolated qualitative evaluation against the configured model."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from career_agent.skill_loader import SkillLoader
from career_agent.web_service import WebService


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        service = WebService(directory, "qwen")
        service.loader = SkillLoader(ROOT / "skills")
        try:
            application_id = service.create_app("星河科技", "增长产品经理")["application_id"]
            service.save_stage(
                application_id,
                {
                    "name": "业务二面",
                    "stage_type": "business",
                    "status": "planned",
                    "focus_areas": ["增长项目", "数据归因", "个人贡献"],
                    "feedback": ["一面认为数据归因和个人贡献讲得不够清楚"],
                    "risks": ["数据归因解释不充分", "个人贡献边界不清晰"],
                    "make_current": True,
                },
            )
            evidence_text = (
                "我负责实验设计，用用户 ID 随机分流，对照比较了 12 万用户两周数据；"
                "新用户次周留存提升 3.2 个百分点。我推动产品、数据和研发统一指标口径。"
            )
            proposed = service.agent_message("补充证据：" + evidence_text, application_id)
            service.confirm_agent_proposal(proposed["proposal"]["proposal_id"], application_id)

            cases = [
                ("普通问答", "什么是机会成本？请用一个生活中的例子解释。"),
                ("岗位辅导", "结合我的经历和业务二面要求，我应该怎样回答数据归因问题？"),
                ("显式面试辅导", "针对当前岗位的面试，请结合我的证据，帮我组织一段数据归因回答。"),
                ("防止编造", "我有多少年增长产品经验？请直接告诉我。"),
                ("追问能力", "如果面试官质疑留存提升来自同期活动，我该怎么回应？"),
            ]
            selected = os.getenv("EVAL_CASE")
            if selected:
                cases = [case for case in cases if case[0] == selected]
            if os.getenv("ROUTER_ONLY") and cases:
                label, question = cases[0]
                try:
                    decision = service._semantic_agent_intent(
                        question, application_id, service.agent_thread(application_id)
                    )
                    print(f"{label}: {decision.model_dump()}")
                except Exception as exc:
                    print(f"{label} router error: {type(exc).__name__}: {exc}")
                return
            for label, question in cases:
                result = service.agent_message(question, application_id)
                answer = result["message"]
                print(f"\n===== {label} =====")
                print("问题：" + question)
                print("意图：" + answer["intent"])
                print("证据：" + ", ".join(item["evidence_id"] for item in answer.get("citations", [])))
                print("回答：\n" + answer["content"])
        finally:
            service.tasks.close()


if __name__ == "__main__":
    main()
