from __future__ import annotations

import argparse
from pathlib import Path

from career_agent.repository import JsonStateRepository
from career_agent.resume_renderer import render_claim_audit_markdown, render_resume_markdown


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a saved tailored resume without an API call")
    parser.add_argument("application_id")
    args = parser.parse_args()

    repository = JsonStateRepository(PROJECT_ROOT / "data" / "applications")
    state = repository.load(args.application_id)
    if not state.tailored_resume_structured:
        raise RuntimeError("Saved State does not contain a structured tailored resume")

    state.tailored_resume = render_resume_markdown(state.tailored_resume_structured)
    state.record("resume_rendered", renderer_version="1.4.3.1")
    repository.save(state)

    output_directory = PROJECT_ROOT / "outputs" / args.application_id
    output_directory.mkdir(parents=True, exist_ok=True)
    resume_path = output_directory / "tailored_resume.md"
    audit_path = output_directory / "resume_evidence_audit.md"
    resume_path.write_text(state.tailored_resume, encoding="utf-8")
    audit_path.write_text(
        render_claim_audit_markdown(
            state.resume_claim_audit or [],
            state.tailored_resume_structured.get("language", "zh-CN"),
        ),
        encoding="utf-8",
    )
    print("No API call was made.")
    print(f"Readable resume: {resume_path}")
    print(f"Evidence audit: {audit_path}")


if __name__ == "__main__":
    main()
