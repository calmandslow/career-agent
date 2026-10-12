"""Review saved answers without regenerating them. API calls are explicit."""
import argparse
import json
import uuid
from pathlib import Path
from career_agent.answer_semantic import hard_check, review_answer, review_is_pass, review_key, has_passed_review
from career_agent.context_builder import build_context
from career_agent.executor_factory import create_executor
from career_agent.repository import JsonStateRepository
from career_agent.reporting import write_run_reports
from career_agent.schemas import AnswerPrepProfile
from career_agent.telemetry import ProgressReporter

PROJECT_ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description="LLM semantic review of saved answers; no regeneration")
    parser.add_argument("application_id")
    parser.add_argument("--question", type=int, help="Review just one question (1-based)")
    parser.add_argument("--force", action="store_true", help="Re-review unchanged passed answers")
    args = parser.parse_args()
    repo = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repo.load(args.application_id)
    profile = AnswerPrepProfile.model_validate(state.pending_validation_output or state.answer_prep_profile)
    indexes = list(range(len(profile.answers))) if args.question is None else [args.question - 1]
    if any(i < 0 or i >= len(profile.answers) for i in indexes):
        raise ValueError("Invalid question number")
    context = build_context("interview-answer-coach", state)
    run_id = "answer_review_" + uuid.uuid4().hex[:12]
    directory = PROJECT_ROOT / "outputs" / args.application_id / "answer_updates" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    executor = None
    result = {"status": "reviewed", "run_id": run_id, "reviews": []}
    try:
        for index in indexes:
            answer = profile.answers[index]
            cached = has_passed_review(state, answer)
            if cached and not args.force:
                print(f"Question {index + 1}: unchanged passed answer, skipped")
                continue
            hard_check(answer, context)
            if executor is None:
                executor = create_executor("qwen", progress=ProgressReporter(enabled=True))
                executor.set_run_id(run_id)
            review = review_answer(executor, answer, context)
            item = {"question_number": index + 1, "answer_hash": review_key(answer, state), **review.model_dump()}
            result["reviews"].append(item)
            state.record("answer_semantic_review", **item)
            if review_is_pass(review):
                state.record("answer_semantic_passed", answer_index=index, answer_hash=review_key(answer, state))
            print(f"Question {index + 1}: {review.verdict}")
            (directory / "semantic_reviews.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            repo.save(state)
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        if executor is not None:
            state.llm_calls.extend(executor.drain_call_records())
        repo.save(state)
        write_run_reports(directory, state, result, run_id=run_id)
    print(f"Review reports: {directory}; answers are unchanged.")

if __name__ == "__main__":
    main()
