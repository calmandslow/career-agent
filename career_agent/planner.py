from __future__ import annotations

from .models import ApplicationState, Intent, PlanStep


def _question_pipeline(state: ApplicationState) -> list[PlanStep]:
    if not state.jd:
        return [PlanStep.ask_user("jd", "需要岗位 JD 才能开始。")]
    plan: list[PlanStep] = []
    if state.jd_profile is None:
        plan.append(PlanStep.skill("jd-analyzer"))
    if state.candidate_role_profile is None or state.tailored_resume is None:
        if not state.base_resume:
            plan.append(PlanStep.ask_user("base_resume", "需要基础简历来完成岗位匹配。"))
            return plan
        plan.append(PlanStep.skill("resume-tailor"))
    if state.interview_research_profile is None:
        plan.append(PlanStep.skill("interview-research"))
    if state.interview_prep_profile is None:
        plan.append(PlanStep.skill("interview-question-generator"))
    return plan


def make_plan(intent: Intent, state: ApplicationState) -> list[PlanStep]:
    if intent is Intent.REVIEW_INTERVIEW:
        if not state.interview_record:
            return [PlanStep.ask_user("interview_record", "需要真实或模拟面试记录才能复盘。")]
        return [] if state.interview_review_profile is not None else [PlanStep.skill("interview-review")]

    if intent is Intent.ANALYZE_JD:
        if not state.jd:
            return [PlanStep.ask_user("jd", "需要岗位 JD 才能开始分析。")]
        return [] if state.jd_profile is not None else [PlanStep.skill("jd-analyzer")]

    pipeline = _question_pipeline(state)
    allowed = {
        Intent.TAILOR_RESUME: {"jd-analyzer", "resume-tailor"},
        Intent.RESEARCH_INTERVIEW: {"jd-analyzer", "resume-tailor", "interview-research"},
    }
    if intent in allowed:
        return [step for step in pipeline if step.field or step.name in allowed[intent]]
    if intent is Intent.GENERATE_INTERVIEW_QUESTIONS:
        return pipeline
    if intent in {Intent.PREPARE_ANSWER, Intent.MOCK_INTERVIEW}:
        plan = list(pipeline)
        if any(step.field for step in plan):
            return plan
        if state.answer_prep_profile is None:
            plan.append(PlanStep.skill("interview-answer-coach"))
        if intent is Intent.MOCK_INTERVIEW and state.mock_interview_profile is None:
            plan.append(PlanStep.skill("mock-interview"))
        return plan
    raise ValueError(f"Unsupported intent: {intent}")
