from __future__ import annotations

from typing import Any

from .models import ApplicationState


def build_context(skill_name: str, state: ApplicationState) -> dict[str, Any]:
    if skill_name == "jd-analyzer":
        return {"jd": state.jd, "company": state.company}
    if skill_name == "resume-tailor":
        return {
            "jd_profile": state.jd_profile,
            "candidate_evidence": state.material_evidence,
            "output_language": _preferred_language(state.jd or ""),
        }
    if skill_name == "interview-research":
        return {"jd_profile": state.jd_profile, "company": state.company, "role": state.role}
    if skill_name == "interview-question-generator":
        return {
            "jd_profile": state.jd_profile,
            "candidate_role_profile": state.candidate_role_profile,
            "interview_research_profile": state.interview_research_profile,
        }
    if skill_name == "interview-answer-coach":
        return {
            "interview_prep_profile": state.interview_prep_profile,
            "candidate_role_profile": state.candidate_role_profile,
            "jd_profile": state.jd_profile,
            "candidate_evidence": state.material_evidence,
            "tailored_resume": state.tailored_resume_structured,
            "resume_claim_audit": state.resume_claim_audit,
            "output_language": _preferred_language(state.jd or ""),
            "answer_scope": "P0_first",
        }
    if skill_name == "mock-interview":
        return {
            "interview_prep_profile": state.interview_prep_profile,
            "answer_prep_profile": state.answer_prep_profile,
        }
    if skill_name == "interview-review":
        return {
            "interview_record": state.interview_record,
            "jd_profile": state.jd_profile,
            "interview_prep_profile": state.interview_prep_profile,
        }
    raise ValueError(f"Unsupported skill: {skill_name}")


def _preferred_language(text: str) -> str:
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return "zh-CN" if chinese >= max(10, latin // 3) else "en"
