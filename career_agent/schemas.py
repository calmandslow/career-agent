from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Responsibility(BaseModel):
    item: str
    priority: str
    evidence: str


class Requirement(BaseModel):
    item: str
    category: str
    priority: str
    evidence: str


class EvaluationDimension(BaseModel):
    dimension: str
    why_it_matters: str
    signals: list[str]


class JDProfile(BaseModel):
    company: str | None = None
    role_title: str
    role_summary: str
    seniority: str | None = None
    responsibilities: list[Responsibility]
    requirements: list[Requirement]
    keywords: list[str]
    evaluation_dimensions: list[EvaluationDimension]
    hidden_expectations: list[str]
    ambiguities: list[str]


class EvidenceBackedStrength(BaseModel):
    requirement: str
    strength: str
    evidence_ids: list[str]


class CandidateGap(BaseModel):
    requirement: str
    gap: str
    risk: str


class ResumeEvidence(BaseModel):
    resume_statement: str
    evidence_ids: list[str]


class TailoredResumeItem(BaseModel):
    text: str
    evidence_ids: list[str]
    achievement_status: Literal["identity", "completed", "observed", "designed", "planned"]


class TailoredResumeEntry(BaseModel):
    heading: str
    subheading: str | None = None
    date_or_location: str | None = None
    evidence_ids: list[str]
    bullets: list[TailoredResumeItem] = Field(min_length=1)


class TailoredResumeSection(BaseModel):
    title: str
    entries: list[TailoredResumeEntry] = Field(min_length=1)


class TailoredResumeDocument(BaseModel):
    language: Literal["zh-CN", "en"]
    candidate_name: str | None = None
    contact_items: list[TailoredResumeItem]
    target_role: str
    professional_summary: TailoredResumeItem
    sections: list[TailoredResumeSection] = Field(min_length=3)


class ResumeClaimAudit(BaseModel):
    claim: str
    claim_type: Literal["identity", "exact_fact", "reframed_fact", "bounded_inference"]
    achievement_status: Literal["identity", "completed", "observed", "designed", "planned"]
    evidence_ids: list[str]
    rationale: str


class CandidateRoleProfile(BaseModel):
    fit_level: Literal["strong", "partial", "weak"]
    fit_summary: str
    matched_strengths: list[EvidenceBackedStrength]
    gaps: list[CandidateGap]
    positioning: list[str]


class ResumeTailorOutput(BaseModel):
    tailored_resume: TailoredResumeDocument
    candidate_role_profile: CandidateRoleProfile
    resume_evidence: list[ResumeEvidence]
    claim_audit: list[ResumeClaimAudit]
    change_notes: list[str]


class SemanticClaimReview(BaseModel):
    claim_id: str
    verdict: Literal["supported", "ambiguous", "overstated", "unsupported"]
    supporting_evidence_ids: list[str]
    rationale: str
    suggested_rewrite: str | None = None


class ResumeSemanticReview(BaseModel):
    reviews: list[SemanticClaimReview]


class ResearchFinding(BaseModel):
    finding: str
    confidence: str
    source_type: str


class InterviewResearchProfile(BaseModel):
    research_mode: str
    company: str | None = None
    role: str | None = None
    findings: list[ResearchFinding]
    likely_process: list[str]
    limitations: list[str]


class InterviewQuestion(BaseModel):
    priority: str
    question: str
    why_likely: str
    evidence_basis: list[str]
    follow_ups: list[str]


class InterviewPrepProfile(BaseModel):
    questions: list[InterviewQuestion]
    focus_areas: list[str]
    high_risk_topics: list[str]


class PreparedAnswer(BaseModel):
    answer_mode: Literal["auto", "experience", "hypothetical", "mixed"] = "auto"
    hypothetical_assumptions: list[str] = Field(default_factory=list)
    question: str
    priority: Literal["P0", "P1", "P2"] = "P0"
    question_type: str = "general"
    evaluation_dimension: str = ""
    evidence_status: Literal["sufficient", "partial", "insufficient"] = "partial"
    answer_structure: list[str]
    evidence_to_use: list[str]
    missing_information: list[str] = Field(default_factory=list)
    answer_skeleton: list[str] = Field(default_factory=list)
    recommended_answer: str = ""
    likely_follow_ups: list[str] = Field(default_factory=list)
    answer_risks: list[str] = Field(default_factory=list)
    gaps_to_avoid_inventing: list[str]


class AnswerPrepProfile(BaseModel):
    answers: list[PreparedAnswer]
    delivery_tips: list[str]
    target_role: str | None = None
    primary_evidence_stories: list[str] = Field(default_factory=list)
    questions_with_missing_evidence: list[str] = Field(default_factory=list)
    high_risk_follow_ups: list[str] = Field(default_factory=list)
    topics_ready_for_mock_interview: list[str] = Field(default_factory=list)


class MockInterviewTurn(BaseModel):
    question: str
    simulated_answer_note: str
    feedback: str


class MockInterviewProfile(BaseModel):
    turns: list[MockInterviewTurn]
    overall_feedback: str
    next_actions: list[str]


class InterviewReviewProfile(BaseModel):
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    likely_failure_points: list[str]
    action_items: list[str]


OUTPUT_MODELS = {
    "jd-analyzer": JDProfile,
    "resume-tailor": ResumeTailorOutput,
    "interview-research": InterviewResearchProfile,
    "interview-question-generator": InterviewPrepProfile,
    "interview-answer-coach": AnswerPrepProfile,
    "mock-interview": MockInterviewProfile,
    "interview-review": InterviewReviewProfile,
}
