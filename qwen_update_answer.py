from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from career_agent.answer_semantic import reviewed_candidate, review_key, has_passed_review, review_is_pass
from career_agent.answer_exports import write_answer_exports
from career_agent.answer_update import focused_context, merge_answer
from career_agent.executor_factory import create_executor
from career_agent.repository import JsonStateRepository
from career_agent.reporting import write_run_reports
from career_agent.schemas import PreparedAnswer
from career_agent.skill_loader import SkillLoader
from career_agent.telemetry import ProgressReporter

PROJECT_ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Update ONE saved answer; existing answers remain untouched")
    parser.add_argument("application_id")
    parser.add_argument("--question", required=True, type=int, help="1-based answer number")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--feedback", help="Uses the configured model for this one answer")
    mode.add_argument("--edited-json", type=Path, help="One human-edited PreparedAnswer; semantic review uses API")
    args = parser.parse_args()
    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repository.load(args.application_id)
    # Validate number and context before any billable request.
    context = focused_context(state, args.question - 1, args.feedback or "Human edited answer")
    run_id = "answer_update_" + uuid.uuid4().hex[:12]
    executor = None
    directory = PROJECT_ROOT / "outputs" / args.application_id
    result = {"status": "failed", "run_id": run_id, "application_id": args.application_id}
    try:
        if args.edited_json:
            replacement = PreparedAnswer.model_validate(json.loads(args.edited_json.read_text(encoding="utf-8")))
        else:
            progress = ProgressReporter(enabled=True)
            executor = create_executor("qwen", progress=progress)
            executor.set_run_id(run_id)
            replacement = executor.execute(SkillLoader(PROJECT_ROOT / "skills").load("interview-answer-coach"), context, PreparedAnswer)
        # Retain the billable generation even when local validation rejects it.
        candidate_directory = directory / "answer_updates" / run_id
        candidate_directory.mkdir(parents=True, exist_ok=True)
        (candidate_directory / "candidate_answer.json").write_text(
            json.dumps(replacement.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Generated candidate saved: {candidate_directory / 'candidate_answer.json'}")
        if replacement.question != context["target_question"] or replacement.priority != context["previous_answer"].get("priority", "P0"):
            raise ValueError("Single-answer update must preserve question and priority")
        if executor is None:
            executor = create_executor("qwen", progress=ProgressReporter(enabled=True))
            executor.set_run_id(run_id)
        replacement, review = reviewed_candidate(executor,
            SkillLoader(PROJECT_ROOT / "skills").load("interview-answer-coach"), replacement,
            context, candidate_directory, allow_repair=not bool(args.edited_json))
        if not review_is_pass(review):
            result["status"] = "semantic_review_pending"
            print("Semantic review did not pass; previous answers retained. Review saved with candidate.")
            return
        result.update(merge_answer(state, args.question - 1, replacement, hard_only=True))
        state.record("answer_semantic_passed", answer_hash=review_key(replacement, state), answer_index=args.question - 1)
        paths = write_answer_exports(directory, state)
        print(f"Updated question {args.question}; answers remain pending review. Readable: {paths['readable']}")
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        print("Update failed; previous answers retained.")
        raise
    finally:
        if executor is not None:
            for call in executor.drain_call_records():
                state.llm_calls.append({**call, "run_id": run_id})
        repository.save(state)
        # Separate report directory avoids overwriting the earlier complete-run cost report.
        write_run_reports(directory / "answer_updates" / run_id, state, result, run_id=run_id)


if __name__ == "__main__":
    main()
