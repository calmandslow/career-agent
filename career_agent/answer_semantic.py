"""Generic, evidence-grounded answer review with bounded single-answer repair."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field
from .schemas import AnswerPrepProfile, PreparedAnswer
from .skill_loader import LoadedSkill
from .verifier import AnswerVerificationError, VerificationError, verify_skill_output

class ReviewIssue(BaseModel):
    code: str
    quote: str
    reason: str
    suggestion: str
    severity: Literal["blocking", "warning"] = "blocking"

class AnswerReview(BaseModel):
    verdict: Literal["pass", "revise", "human_review"]
    issues: list[ReviewIssue] = Field(default_factory=list)


def fingerprint(answer):
    data = answer.model_dump() if isinstance(answer, BaseModel) else answer
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def hard_check(answer, context):
    verify_skill_output("interview-answer-coach", AnswerPrepProfile(answers=[answer], delivery_tips=[]),
                        context={**context, "answer_hard_only": True})


def review_answer(executor, answer, context):
    policy = (Path(__file__).resolve().parents[1] / "config" / "answer_review_policy.md").read_text(encoding="utf-8")
    cited = set(answer.evidence_to_use)
    evidence = {key: [item for item in items if isinstance(item, dict) and item.get("evidence_id") in cited]
                for key, items in (context.get("candidate_evidence") or {}).items() if isinstance(items, list)}
    result = executor.execute(LoadedSkill("answer-semantic-verifier", policy),
                              {"answer": answer.model_dump(), "candidate_evidence": evidence,
                               "jd_profile": context.get("jd_profile")}, AnswerReview)
    # Never accept a contradictory verdict or an ungrounded reported quotation.
    if result.verdict == "pass" and any(i.severity == "blocking" for i in result.issues):
        raise VerificationError("Semantic review returned contradictory pass verdict")
    serialized = json.dumps(answer.model_dump(), ensure_ascii=False)
    if any(not i.quote or i.quote not in serialized for i in result.issues):
        raise VerificationError("Semantic review issue must quote the supplied answer exactly")
    return result


def reviewed_candidate(executor, skill, answer, context, directory, *, allow_repair=True):
    directory.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        (directory / name).write_text(json.dumps(value.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    save("candidate_answer.json", answer)
    hard_check(answer, context)
    review = review_answer(executor, answer, context)
    save("semantic_review.json", review)
    if review.verdict == "revise" and allow_repair:
        repaired = executor.execute(skill, {**context, "previous_answer": answer.model_dump(),
            "verification_feedback": review.model_dump(),
            "repair_instruction": "Return ONE corrected PreparedAnswer. Preserve question and priority. Address review issues only; do not invent facts."}, PreparedAnswer)
        save("repaired_candidate.json", repaired)
        if repaired.question != answer.question or repaired.priority != answer.priority:
            raise VerificationError("Semantic repair changed question or priority")
        hard_check(repaired, context)
        answer = repaired
        review = review_answer(executor, answer, context)
        save("final_semantic_review.json", review)
    return answer, review


def review_is_pass(review):
    return review.verdict == "pass" and not any(i.severity == "blocking" for i in review.issues)


def review_key(answer, state):
    return fingerprint({"answer": answer.model_dump(), "evidence": state.material_evidence, "jd": state.jd_profile,
                        "policy": (Path(__file__).resolve().parents[1] / "config" / "answer_review_policy.md").read_text(encoding="utf-8")})


def has_passed_review(state, answer):
    key = review_key(answer, state)
    for event in reversed(state.execution_history):
        if event.detail.get("answer_hash") != key:
            continue
        if event.event == "answer_semantic_passed":
            return True
        if event.event == "answer_semantic_review":
            return False
    return False
