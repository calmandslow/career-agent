from __future__ import annotations

import argparse
import json
from pathlib import Path

from career_agent import ApplicationState, Intent, JsonStateRepository, Orchestrator
from career_agent.executor import OpenAICompatibleChatExecutor
from career_agent.executor_factory import create_executor
from career_agent.material_loader import MaterialLoader
from career_agent.skill_loader import SkillLoader
from career_agent.resume_renderer import render_claim_audit_markdown, render_semantic_review_markdown
from career_agent.reporting import write_run_reports
from career_agent.telemetry import ProgressReporter


PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Career Agent with real local materials")
    parser.add_argument("--jd", action="append", required=True, help="JD text/document/screenshot; repeat for multiple screenshots")
    parser.add_argument("--resume", required=True, help="Base resume file")
    parser.add_argument("--portfolio", action="append", default=[], help="Portfolio file; repeat for multiple files")
    parser.add_argument("--company", default=None, help="Optional company name")
    parser.add_argument("--role", default=None, help="Optional role name")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    progress = ProgressReporter(enabled=True)
    executor = create_executor("qwen", progress=progress)
    if not isinstance(executor, OpenAICompatibleChatExecutor):
        raise RuntimeError("The selected executor cannot read image materials")
    loader = MaterialLoader(image_reader=executor.extract_image_text)

    jd_materials = [loader.load(path) for path in args.jd]
    resume_material = loader.load(args.resume)
    portfolio_materials = [loader.load(path) for path in args.portfolio]

    print("Loaded materials:")
    for material in [*jd_materials, resume_material, *portfolio_materials]:
        print(f"- {material.source} ({material.kind}, {len(material.text)} characters)")

    state = ApplicationState(
        company=args.company,
        role=args.role,
        jd=MaterialLoader.combine(jd_materials),
        base_resume=resume_material.text,
        portfolio=MaterialLoader.combine(portfolio_materials) if portfolio_materials else None,
    )
    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    repository.save(state)
    orchestrator = Orchestrator(
        repository,
        SkillLoader(PROJECT_ROOT / "skills"),
        executor,
        progress=progress,
    )
    output_directory = PROJECT_ROOT / "outputs" / state.application_id

    try:
        result = orchestrator.run(state.application_id, Intent.GENERATE_INTERVIEW_QUESTIONS)
    except Exception as exc:
        failed = repository.load(state.application_id)
        reports = write_run_reports(
            output_directory,
            failed,
            {
                "status": "failed",
                "application_id": state.application_id,
                "error_type": type(exc).__name__,
            },
            run_id=orchestrator.last_run_id,
        )
        print(f"Pipeline stopped at: {failed.current_stage}")
        print(f"State retained in data/applications/{state.application_id}.json")
        print(f"Run report: {reports['run_report']}")
        raise

    completed = repository.load(state.application_id)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / "result.json"
    output_path.write_text(
        json.dumps(
            {
                "run_result": result.to_dict(),
                "materials": {
                    "jd": [material.source for material in jd_materials],
                    "resume": resume_material.source,
                    "portfolio": [material.source for material in portfolio_materials],
                },
                "state": completed.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_directory / "tailored_resume.md").write_text(completed.tailored_resume or "", encoding="utf-8")
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
    print(f"Readable resume: {output_directory / 'tailored_resume.md'}")
    print(f"Evidence audit: {output_directory / 'resume_evidence_audit.md'}")
    print(f"Semantic review: {output_directory / 'resume_semantic_review.md'}")
    print(f"Run report: {reports['run_report']}")
    print(f"Cost report: {reports['cost_report']}")


if __name__ == "__main__":
    main()
