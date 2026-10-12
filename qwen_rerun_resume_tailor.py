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


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-run only Resume Tailor from saved State")
    parser.add_argument("application_id")
    args = parser.parse_args()

    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repository.load(args.application_id)
    if not state.jd_profile or not state.material_evidence:
        raise RuntimeError("Saved State must contain jd_profile and material_evidence")

    state.tailored_resume = None
    state.tailored_resume_structured = None
    state.resume_evidence = None
    state.resume_claim_audit = None
    state.resume_semantic_review = None
    state.resume_semantic_review_history = []
    state.pending_validation_output = None
    state.validation_issues = None
    state.resume_change_notes = None
    state.candidate_role_profile = None
    state.interview_prep_profile = None
    state.answer_prep_profile = None
    state.mock_interview_profile = None
    state.interview_review_profile = None
    state.record("outputs_invalidated", reason="manual_resume_tailor_rerun")
    repository.save(state)

    progress = ProgressReporter(enabled=True)
    orchestrator = Orchestrator(
        repository,
        SkillLoader(PROJECT_ROOT / "skills"),
        create_executor("qwen", progress=progress),
        progress=progress,
    )
    output_directory = PROJECT_ROOT / "outputs" / args.application_id
    try:
        result = orchestrator.run(args.application_id, Intent.TAILOR_RESUME)
    except Exception as exc:
        failed = repository.load(args.application_id)
        reports = write_run_reports(
            output_directory,
            failed,
            {
                "status": "failed",
                "application_id": args.application_id,
                "error_type": type(exc).__name__,
                "run_id": orchestrator.last_run_id,
                "started_at": orchestrator.last_started_at,
            },
            run_id=orchestrator.last_run_id,
        )
        print(f"Pipeline stopped at: {failed.current_stage}")
        print(f"State retained in data/applications/{args.application_id}.json")
        print(f"Run report: {reports['run_report']}")
        raise

    completed = repository.load(args.application_id)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / "resume_tailor_result.json"
    output_path.write_text(
        json.dumps(
            {"run_result": result.to_dict(), "state": completed.to_dict()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    semantic_review_path = output_directory / "resume_semantic_review.md"
    semantic_review_path.write_text(
        render_semantic_review_markdown(completed.resume_semantic_review_history),
        encoding="utf-8",
    )
    reports = write_run_reports(
        output_directory,
        completed,
        result.to_dict(),
        run_id=result.run_id,
    )
    if result.status != "completed":
        review_path = output_directory / "pending_human_review.json"
        review_path.write_text(
            json.dumps(
                {
                    "run_result": result.to_dict(),
                    "validation_issues": completed.validation_issues,
                    "pending_validation_output": completed.pending_validation_output,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Paused: {result.pending_question}")
        print(f"Human review package: {review_path}")
        print(f"Semantic review: {semantic_review_path}")
        print(f"Run report: {reports['run_report']}")
        return
    (output_directory / "tailored_resume.md").write_text(
        completed.tailored_resume or "",
        encoding="utf-8",
    )
    (output_directory / "resume_evidence_audit.md").write_text(
        render_claim_audit_markdown(
            completed.resume_claim_audit or [],
            (completed.tailored_resume_structured or {}).get("language", "zh-CN"),
        ),
        encoding="utf-8",
    )
    print(f"Completed: {result.completed_steps}")
    print(f"Result saved to: {output_path}")
    print(f"Readable resume: {output_directory / 'tailored_resume.md'}")
    print(f"Evidence audit: {output_directory / 'resume_evidence_audit.md'}")
    print(f"Semantic review: {semantic_review_path}")
    print(f"Run report: {reports['run_report']}")
    print(f"Cost report: {reports['cost_report']}")


if __name__ == "__main__":
    main()
