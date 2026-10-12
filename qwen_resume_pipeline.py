from __future__ import annotations

import argparse
import json
from pathlib import Path

from career_agent import Intent, JsonStateRepository, Orchestrator
from career_agent.executor_factory import create_executor
from career_agent.skill_loader import SkillLoader
from career_agent.resume_renderer import render_claim_audit_markdown, render_semantic_review_markdown
from career_agent.reporting import write_run_reports
from career_agent.telemetry import ProgressReporter


PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resume a retained Qwen Career Agent application")
    parser.add_argument("application_id", help="Saved application ID, for example job_38182b62f385")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repository.load(args.application_id)
    print(f"Resuming {args.application_id} from: {state.current_stage}")

    progress = ProgressReporter(enabled=True)
    orchestrator = Orchestrator(
        repository,
        SkillLoader(PROJECT_ROOT / "skills"),
        create_executor("qwen", progress=progress),
        progress=progress,
    )
    output_directory = PROJECT_ROOT / "outputs" / args.application_id
    try:
        result = orchestrator.run(args.application_id, Intent.GENERATE_INTERVIEW_QUESTIONS)
    except Exception as exc:
        failed = repository.load(args.application_id)
        reports = write_run_reports(
            output_directory,
            failed,
            {
                "status": "failed",
                "application_id": args.application_id,
                "error_type": type(exc).__name__,
            },
            run_id=orchestrator.last_run_id,
        )
        print(f"Pipeline stopped at: {failed.current_stage}")
        print(f"State retained in data/applications/{args.application_id}.json")
        print(f"Run report: {reports['run_report']}")
        raise

    completed = repository.load(args.application_id)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / "result.json"
    output_path.write_text(
        json.dumps(
            {
                "run_result": result.to_dict(),
                "materials": {"recovered_from_saved_state": True},
                "state": completed.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if completed.tailored_resume:
        (output_directory / "tailored_resume.md").write_text(completed.tailored_resume, encoding="utf-8")
        (output_directory / "resume_evidence_audit.md").write_text(
            render_claim_audit_markdown(completed.resume_claim_audit or [], (completed.tailored_resume_structured or {}).get("language", "zh-CN")),
            encoding="utf-8",
        )
        (output_directory / "resume_semantic_review.md").write_text(
            render_semantic_review_markdown(completed.resume_semantic_review_history),
            encoding="utf-8",
        )
    reports = write_run_reports(
        output_directory,
        completed,
        result.to_dict(),
        run_id=result.run_id,
    )
    print(f"Completed: {result.completed_steps}")
    print(f"Result saved to: {output_path}")
    print(f"Run report: {reports['run_report']}")
    print(f"Cost report: {reports['cost_report']}")


if __name__ == "__main__":
    main()
