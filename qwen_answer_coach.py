from __future__ import annotations

import argparse
import json
from pathlib import Path

from career_agent import Intent, JsonStateRepository, Orchestrator
from career_agent.executor_factory import create_executor
from career_agent.reporting import write_run_reports
from career_agent.answer_exports import write_answer_exports
from career_agent.skill_loader import SkillLoader
from career_agent.telemetry import ProgressReporter

PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare truthful P0 interview answers from saved State")
    parser.add_argument("application_id")
    parser.add_argument("--force", action="store_true", help="Explicitly regenerate completed or pending answers (uses API)")
    args = parser.parse_args()
    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repository.load(args.application_id)
    if not state.interview_prep_profile:
        raise RuntimeError("Saved State must contain interview_prep_profile")
    if not args.force and state.pending_validation_output and state.current_stage == "waiting_for_human_review_interview-answer-coach":
        output_directory = PROJECT_ROOT / "outputs" / args.application_id
        paths = write_answer_exports(output_directory, state)
        print("Pending answers retained. No model API call was made.")
        print(f"Review: {paths['readable']}")
        print("Use python qwen_review_answers.py APPLICATION_ID for semantic review (API); recheck_answer_coach.py is local only; --force regenerates.")
        return
    if not args.force and state.answer_prep_profile:
        paths = write_answer_exports(PROJECT_ROOT / "outputs" / args.application_id, state)
        print(f"Existing answers retained. No model API call was made. Readable answers: {paths['readable']}")
        return
    state.answer_prep_profile = None
    state.mock_interview_profile = None
    state.record("outputs_invalidated", reason="manual_answer_coach_rerun")
    repository.save(state)
    progress = ProgressReporter(enabled=True)
    orchestrator = Orchestrator(repository, SkillLoader(PROJECT_ROOT / "skills"), create_executor("qwen", progress=progress), progress=progress)
    output_directory = PROJECT_ROOT / "outputs" / args.application_id
    try:
        result = orchestrator.run(args.application_id, Intent.PREPARE_ANSWER)
    except Exception as exc:
        failed = repository.load(args.application_id)
        write_run_reports(output_directory, failed, {"status": "failed", "application_id": args.application_id, "error_type": type(exc).__name__, "run_id": orchestrator.last_run_id, "started_at": orchestrator.last_started_at}, run_id=orchestrator.last_run_id)
        print(f"Pipeline stopped at: {failed.current_stage}")
        raise
    completed = repository.load(args.application_id)
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = write_answer_exports(output_directory, completed)
    reports = write_run_reports(output_directory, completed, result.to_dict(), run_id=result.run_id)
    print(f"Completed: {result.completed_steps}")
    print(f"Status: {result.status}")
    print(f"Answer file: {paths['profile']}")
    print(f"Readable answers: {paths['readable']}")
    print(f"Run report: {reports['run_report']}")


if __name__ == "__main__":
    main()
