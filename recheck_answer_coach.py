"""Recheck saved pending answers locally; never instantiate a model client."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from career_agent.answer_semantic import review_key, has_passed_review
from career_agent.answer_exports import write_answer_exports
from career_agent.context_builder import build_context
from career_agent.orchestrator import Orchestrator
from career_agent.repository import JsonStateRepository
from career_agent.schemas import AnswerPrepProfile
from career_agent.verifier import VerificationError, verify_skill_output

PROJECT_ROOT = Path(__file__).resolve().parent


def recheck(state, *, approve=False):
    if state.current_stage != "waiting_for_human_review_interview-answer-coach":
        raise RuntimeError("State is not waiting for Answer Coach review")
    output = AnswerPrepProfile.model_validate(state.pending_validation_output)
    old_issues = list(state.validation_issues or [])
    try:
        verify_skill_output("interview-answer-coach", output,
                            context={**build_context("interview-answer-coach", state), "answer_hard_only": True})
    except VerificationError as exc:
        state.validation_issues = [str(exc)]
        state.record("answer_local_recheck", error="; ".join(old_issues), status="failed")
        return {"status": "waiting_for_user", "previous_issues": old_issues,
                "current_issues": state.validation_issues, "api_calls": 0}
    state.validation_issues = []
    state.record("answer_local_recheck", error="; ".join(old_issues), status="passed")
    if approve:
        missing = [i + 1 for i, answer in enumerate(output.answers) if not has_passed_review(state, answer)]
        if missing:
            return {"status": "semantic_review_required", "question_numbers": missing, "api_calls": 0}
        Orchestrator._apply_output("interview-answer-coach", output.model_dump(), state)
        state.pending_validation_output = None
        state.validation_issues = None
        state.current_stage = "completed"
        state.mock_interview_profile = None
        state.record("human_review_approved", skill="interview-answer-coach")
        state.record("skill_completed", skill="interview-answer-coach", completion_mode="human_review")
    return {"status": "completed" if approve else "local_validation_passed",
            "previous_issues": old_issues, "current_issues": [], "api_calls": 0}


def main():
    parser = argparse.ArgumentParser(description="Local validation of pending Answer Coach output; no API")
    parser.add_argument("application_id")
    parser.add_argument("--approve", action="store_true", help="Confirm human review and commit if local checks pass")
    args = parser.parse_args()
    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repository.load(args.application_id)
    result = recheck(state, approve=args.approve)
    repository.save(state)
    directory = PROJECT_ROOT / "outputs" / args.application_id
    paths = write_answer_exports(directory, state)
    (directory / "answer_local_recheck.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Readable answers: {paths['readable']}")
    if result["status"] == "local_validation_passed":
        print("Local checks passed; answers remain pending human review. Use --approve after reviewing them.")


if __name__ == "__main__":
    main()
