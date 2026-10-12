from __future__ import annotations

from copy import deepcopy

from .answer_policy import infer_answer_mode
from .context_builder import build_context
from .schemas import AnswerPrepProfile, PreparedAnswer
from .verifier import VerificationError, verify_skill_output


def focused_context(state, index: int, feedback: str) -> dict:
    profile = state.pending_validation_output if state.current_stage == "waiting_for_human_review_interview-answer-coach" else state.answer_prep_profile
    if not profile or not 0 <= index < len(profile["answers"]):
        raise ValueError("Question number is outside the saved answer list")
    target = profile["answers"][index]
    cited = set(target.get("evidence_to_use", []))
    evidence = {source: [item for item in items if isinstance(item, dict) and item.get("evidence_id") in cited]
                for source, items in (state.material_evidence or {}).items() if isinstance(items, list)}
    return {"target_question": target["question"], "previous_answer": target,
            "question_intent": infer_answer_mode(target["question"]),
            "candidate_evidence": evidence, "jd_profile": state.jd_profile,
            "verification_feedback": feedback,
            "repair_instruction": "Return ONE PreparedAnswer. Improve only this answer according to feedback and question_intent. Preserve source question and priority. No new unsupported experience. Explicitly label hypothetical parts. Update skeleton, risks, assumptions and followups consistently."}


def merge_answer(state, index: int, replacement: PreparedAnswer, *, hard_only=False) -> dict:
    """Stage a single edited answer; do not automatically grant human approval."""
    current = state.pending_validation_output if state.current_stage == "waiting_for_human_review_interview-answer-coach" else state.answer_prep_profile
    if not current or not 0 <= index < len(current["answers"]):
        raise ValueError("Question number is outside the saved answer list")
    previous = deepcopy(current)
    target = previous["answers"][index]
    if replacement.question != target["question"] or replacement.priority != target.get("priority", "P0"):
        raise ValueError("Single-answer update must preserve question and priority")
    if replacement.answer_mode == "auto":
        replacement = replacement.model_copy(update={"answer_mode": infer_answer_mode(replacement.question)})
    # Validate the replacement first. A bad API output cannot replace the saved answer.
    context = {**build_context("interview-answer-coach", state), "answer_hard_only": hard_only}
    single = AnswerPrepProfile(answers=[replacement], delivery_tips=[])
    verify_skill_output("interview-answer-coach", single, context=context)
    candidate = deepcopy(previous)
    candidate["answers"][index] = replacement.model_dump()
    # Rebuild derived handoff lists; retain unrelated per-answer data verbatim.
    answers = candidate["answers"]
    candidate["questions_with_missing_evidence"] = [a["question"] for a in answers if a.get("evidence_status") != "sufficient"]
    candidate["high_risk_follow_ups"] = list(dict.fromkeys(x for a in answers for x in a.get("likely_follow_ups", [])))
    candidate["topics_ready_for_mock_interview"] = list(dict.fromkeys(a.get("evaluation_dimension", "") for a in answers if a.get("recommended_answer") and a.get("evaluation_dimension")))
    issues = []
    try:
        verify_skill_output("interview-answer-coach", AnswerPrepProfile.model_validate(candidate), context=context)
    except VerificationError as exc:
        issues = [str(exc)]
    state.pending_validation_output = candidate
    state.validation_issues = issues
    state.current_stage = "waiting_for_human_review_interview-answer-coach"
    state.answer_prep_profile = None
    state.mock_interview_profile = None
    state.record("answer_updated", answer_index=index, previous_answer=target,
                 skill="interview-answer-coach", completion_mode="pending_human_review")
    return {"status": "waiting_for_user", "question_number": index + 1, "current_issues": issues}
