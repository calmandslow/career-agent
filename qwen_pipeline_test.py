from __future__ import annotations

import json
import tempfile
from pathlib import Path

from career_agent import ApplicationState, Intent, JsonStateRepository, Orchestrator
from career_agent.executor_factory import create_executor
from career_agent.skill_loader import SkillLoader
from career_agent.telemetry import ProgressReporter


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    with tempfile.TemporaryDirectory() as state_directory:
        repository = JsonStateRepository(state_directory)
        state = ApplicationState(
            company="示例 AI 公司",
            role="AI Agent 产品经理",
            jd=(
                "负责 AI Agent 产品规划与需求分析，设计从用户目标理解、任务规划到执行反馈的完整流程；"
                "与算法、研发和设计团队协作推动产品上线；建立效果评估指标并基于数据持续迭代。"
                "要求具备 AI 产品或大模型应用经验，理解 LLM、Prompt、RAG、Agent 等基本概念，"
                "具备良好的数据分析、跨团队协作和产品表达能力。"
            ),
            base_resume=(
                "教育背景：应用数学本科，应用统计硕士。\n"
                "项目经历：设计并搭建求职 Career Agent，采用确定性 Planner、State、Context Builder、"
                "Skill、Executor 和 Verifier 串联 JD 分析、简历调整和面试准备流程；"
                "先以 Mock Executor 验证 Harness，再接入真实模型 API。\n"
                "其他项目：PickBetter 消费决策清单，完成用户场景、竞品分析、核心流程和低保真原型。\n"
                "技能：Python 基础、数据分析、AI 产品设计、PRD、Figma。"
            ),
            portfolio=(
                "Career Agent：完成 Mock Harness、15 项自动化测试、千问结构化输出接入；"
                "强调可验证、可切换模型和不虚构候选人经历。"
            ),
        )
        repository.save(state)

        progress = ProgressReporter(enabled=True)
        orchestrator = Orchestrator(
            repository,
            SkillLoader(PROJECT_ROOT / "skills"),
            create_executor("qwen", progress=progress),
            progress=progress,
        )

        print("Running four core skills plus mandatory semantic review and optional repair calls...")
        try:
            result = orchestrator.run(state.application_id, Intent.GENERATE_INTERVIEW_QUESTIONS)
        except Exception:
            failed = repository.load(state.application_id)
            print(f"Pipeline stopped at: {failed.current_stage}")
            raise

        completed = repository.load(state.application_id)
        report = {
            "run_result": result.to_dict(),
            "current_stage": completed.current_stage,
            "jd_profile": completed.jd_profile,
            "tailored_resume": completed.tailored_resume,
            "candidate_role_profile": completed.candidate_role_profile,
            "resume_semantic_review_history": completed.resume_semantic_review_history,
            "interview_research_profile": completed.interview_research_profile,
            "interview_prep_profile": completed.interview_prep_profile,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
