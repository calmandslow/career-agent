from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Intent(str, Enum):
    ANALYZE_JD = "analyze_jd"
    TAILOR_RESUME = "tailor_resume"
    RESEARCH_INTERVIEW = "research_interview"
    GENERATE_INTERVIEW_QUESTIONS = "generate_interview_questions"
    PREPARE_ANSWER = "prepare_answer"
    MOCK_INTERVIEW = "mock_interview"
    REVIEW_INTERVIEW = "review_interview"


class StepType(str, Enum):
    SKILL = "skill"
    TOOL = "tool"
    ASK_USER = "ask_user"


@dataclass(slots=True)
class PlanStep:
    type: StepType
    name: str | None = None
    field: str | None = None
    reason: str | None = None

    @classmethod
    def skill(cls, name: str) -> "PlanStep":
        return cls(type=StepType.SKILL, name=name)

    @classmethod
    def ask_user(cls, field: str, reason: str) -> "PlanStep":
        return cls(type=StepType.ASK_USER, field=field, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        return {key: value for key, value in data.items() if value is not None}


@dataclass(slots=True)
class ExecutionEvent:
    event: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ApplicationState:
    application_id: str = field(default_factory=lambda: f"job_{uuid4().hex[:12]}")
    company: str | None = None
    role: str | None = None

    jd: str | None = None
    base_resume: str | None = None
    portfolio: str | None = None
    material_evidence: dict[str, Any] | None = None

    jd_profile: dict[str, Any] | None = None
    tailored_resume: str | None = None
    tailored_resume_structured: dict[str, Any] | None = None
    resume_evidence: list[dict[str, Any]] | None = None
    resume_claim_audit: list[dict[str, Any]] | None = None
    resume_semantic_review: dict[str, Any] | None = None
    resume_semantic_review_history: list[dict[str, Any]] = field(default_factory=list)
    pending_validation_output: dict[str, Any] | None = None
    validation_issues: list[str] | None = None
    resume_change_notes: list[str] | None = None
    candidate_role_profile: dict[str, Any] | None = None
    interview_research_profile: dict[str, Any] | None = None
    interview_prep_profile: dict[str, Any] | None = None
    answer_prep_profile: dict[str, Any] | None = None
    mock_interview_profile: dict[str, Any] | None = None
    interview_record: str | None = None
    interview_review_profile: dict[str, Any] | None = None
    recruitment_process: dict[str, Any] | None = None

    current_stage: str = "created"
    execution_history: list[ExecutionEvent] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def record(self, event: str, **detail: Any) -> None:
        self.execution_history.append(ExecutionEvent(event=event, detail=detail))
        self.updated_at = utc_now()

    def invalidate_jd_outputs(self) -> None:
        self.material_evidence = None
        self.jd_profile = None
        self.tailored_resume = None
        self.tailored_resume_structured = None
        self.resume_evidence = None
        self.resume_claim_audit = None
        self.resume_semantic_review = None
        self.resume_semantic_review_history = []
        self.pending_validation_output = None
        self.validation_issues = None
        self.resume_change_notes = None
        self.candidate_role_profile = None
        self.interview_research_profile = None
        self.interview_prep_profile = None
        self.answer_prep_profile = None
        self.mock_interview_profile = None
        self.interview_review_profile = None

    def update_materials(
        self,
        *,
        jd: str | None = None,
        base_resume: str | None = None,
        portfolio: str | None = None,
        interview_record: str | None = None,
    ) -> None:
        updated: list[str] = []
        if jd is not None:
            new_jd = jd.strip() or None
            if new_jd != self.jd:
                self.invalidate_jd_outputs()
            self.jd = new_jd
            updated.append("jd")
        if base_resume is not None:
            new_resume = base_resume.strip() or None
            if new_resume != self.base_resume:
                self.material_evidence = None
                self.tailored_resume = None
                self.tailored_resume_structured = None
                self.resume_evidence = None
                self.resume_claim_audit = None
                self.resume_semantic_review = None
                self.resume_semantic_review_history = []
                self.pending_validation_output = None
                self.validation_issues = None
                self.resume_change_notes = None
                self.candidate_role_profile = None
                self.interview_prep_profile = None
                self.answer_prep_profile = None
                self.mock_interview_profile = None
                self.interview_review_profile = None
            self.base_resume = new_resume
            updated.append("base_resume")
        if portfolio is not None:
            new_portfolio = portfolio.strip() or None
            if new_portfolio != self.portfolio:
                self.material_evidence = None
                self.tailored_resume = None
                self.tailored_resume_structured = None
                self.resume_evidence = None
                self.resume_claim_audit = None
                self.resume_semantic_review = None
                self.resume_semantic_review_history = []
                self.pending_validation_output = None
                self.validation_issues = None
                self.resume_change_notes = None
                self.candidate_role_profile = None
                self.interview_prep_profile = None
                self.answer_prep_profile = None
                self.mock_interview_profile = None
                self.interview_review_profile = None
            self.portfolio = new_portfolio
            updated.append("portfolio")
        if interview_record is not None:
            new_record = interview_record.strip() or None
            if new_record != self.interview_record:
                self.interview_review_profile = None
            self.interview_record = new_record
            updated.append("interview_record")
        self.record("materials_updated", fields=updated)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApplicationState":
        clean = dict(data)
        clean["execution_history"] = [
            item if isinstance(item, ExecutionEvent) else ExecutionEvent(**item)
            for item in clean.get("execution_history", [])
        ]
        return cls(**clean)
