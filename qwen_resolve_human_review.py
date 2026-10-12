from __future__ import annotations

import argparse
import json
from pathlib import Path

from career_agent.human_review import accept_pending_resume_review
from career_agent.repository import JsonStateRepository
from career_agent.reporting import write_run_reports
from career_agent.resume_renderer import render_claim_audit_markdown, render_semantic_review_markdown
from career_agent.resume_truthfulness import apply_conservative_resume_framing
from career_agent.schemas import ResumeTailorOutput


PROJECT_ROOT = Path(__file__).resolve().parent


def _load_edited_output(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "pending_validation_output" in payload:
        return payload["pending_validation_output"]
    if "state" in payload and payload["state"].get("pending_validation_output"):
        return payload["state"]["pending_validation_output"]
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Approve or commit an edited Resume Tailor result without an LLM call"
    )
    parser.add_argument("application_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--approve", action="store_true", help="Approve the pending output as-is")
    mode.add_argument(
        "--apply-conservative",
        action="store_true",
        help="Apply deterministic conservative wording to the pending output and commit it",
    )
    mode.add_argument("--edited-json", type=Path, help="Commit a human-edited ResumeTailorOutput JSON")
    args = parser.parse_args()

    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repository.load(args.application_id)
    edited = _load_edited_output(args.edited_json) if args.edited_json else None
    if args.apply_conservative:
        if not state.pending_validation_output:
            raise RuntimeError("No pending Resume Tailor output is available")
        edited = apply_conservative_resume_framing(
            ResumeTailorOutput.model_validate(state.pending_validation_output)
        ).model_dump()
    accept_pending_resume_review(state, edited_output=edited)
    repository.save(state)

    output_directory = PROJECT_ROOT / "outputs" / args.application_id
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "tailored_resume.md").write_text(state.tailored_resume or "", encoding="utf-8")
    (output_directory / "resume_evidence_audit.md").write_text(
        render_claim_audit_markdown(
            state.resume_claim_audit or [],
            (state.tailored_resume_structured or {}).get("language", "zh-CN"),
        ),
        encoding="utf-8",
    )
    (output_directory / "resume_semantic_review.md").write_text(
        render_semantic_review_markdown(state.resume_semantic_review_history), encoding="utf-8"
    )
    result = {
        "status": "completed",
        "application_id": args.application_id,
        "completed_steps": ["resume-tailor"],
        "completion_mode": "human_review",
    }
    (output_directory / "resume_tailor_result.json").write_text(
        json.dumps({"run_result": result, "state": state.to_dict()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    reports = write_run_reports(output_directory, state, result)
    pending_path = output_directory / "pending_human_review.json"
    if pending_path.exists():
        pending_path.unlink()

    print("Human review resolved. No model API call was made.")
    print(f"Readable resume: {output_directory / 'tailored_resume.md'}")
    print(f"Evidence audit: {output_directory / 'resume_evidence_audit.md'}")
    print(f"Run report: {reports['run_report']}")


if __name__ == "__main__":
    main()
