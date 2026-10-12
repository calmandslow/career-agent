from __future__ import annotations

from typing import Any

from .context_builder import build_context
from .models import ApplicationState
from .orchestrator import Orchestrator
from .schemas import ResumeTailorOutput
from .verifier import verify_skill_output


def accept_pending_resume_review(
    state: ApplicationState,
    *,
    edited_output: dict[str, Any] | None = None,
) -> ResumeTailorOutput:
    """Commit a human-approved pending resume without another model call."""
    if state.current_stage != "waiting_for_human_review_resume-tailor":
        raise RuntimeError("State is not waiting for Resume Tailor human review")
    candidate = edited_output or state.pending_validation_output
    if not candidate:
        raise RuntimeError("No pending Resume Tailor output is available")
    output = ResumeTailorOutput.model_validate(candidate)
    verify_skill_output(
        "resume-tailor",
        output,
        context=build_context("resume-tailor", state),
    )
    Orchestrator._apply_output("resume-tailor", output.model_dump(), state)
    state.pending_validation_output = None
    state.validation_issues = None
    state.current_stage = "completed"
    state.record("human_review_approved", skill="resume-tailor", edited=edited_output is not None)
    state.record("skill_completed", skill="resume-tailor", completion_mode="human_review")
    return output
