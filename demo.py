from pathlib import Path
from tempfile import TemporaryDirectory

from career_agent import ApplicationState, Intent, JsonStateRepository, Orchestrator
from career_agent.mock_executor import MockSkillExecutor
from career_agent.skill_loader import SkillLoader


PROJECT_ROOT = Path(__file__).resolve().parent

with TemporaryDirectory() as directory:
    repository = JsonStateRepository(directory)
    state = ApplicationState(
        company="示例公司",
        role="AI 产品经理",
        jd="负责 AI Agent 产品规划、跨团队交付和效果验证。",
        base_resume="应用统计硕士，有 AI 产品项目经验。",
        portfolio="Career Agent 项目：确定性 Planner + Skills + Verifier。",
    )
    repository.save(state)
    harness = Orchestrator(repository, SkillLoader(PROJECT_ROOT / "skills"), MockSkillExecutor())

    preparation = harness.run(state.application_id, Intent.MOCK_INTERVIEW)
    print("Mock preparation:", preparation.to_dict())

    state = repository.load(state.application_id)
    state.update_materials(interview_record="完成一次 Mock 面试，回答了项目目标与技术取舍。")
    repository.save(state)
    review = harness.run(state.application_id, Intent.REVIEW_INTERVIEW)
    print("Mock review:", review.to_dict())
    print("Final state:", repository.load(state.application_id).current_stage)
