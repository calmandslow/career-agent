from __future__ import annotations

import re
from typing import Any

from .schemas import ResumeSemanticReview, ResumeTailorOutput
from .verifier import VerificationError


class SemanticVerificationError(VerificationError):
    def __init__(self, message: str, *, repairable: bool) -> None:
        super().__init__(message)
        self.repairable = repairable


_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("experience", re.compile(r"经验|多年|years? of experience", re.IGNORECASE)),
    ("zero_to_one", re.compile(r"从\s*0\s*到\s*1|0\s*[-–—]?\s*to\s*1", re.IGNORECASE)),
    ("ownership", re.compile(r"主导|牵头|负责|owner|owned|led|drove", re.IGNORECASE)),
    ("delivery", re.compile(r"上线|落地|交付|发布|launched|shipped|deployed", re.IGNORECASE)),
    ("metric", re.compile(r"提升|增长|降低|节省|转化率|留存率|\d+(?:\.\d+)?\s*(?:%|倍|万|千)?|increased|improved|reduced", re.IGNORECASE)),
    ("collaboration", re.compile(r"跨部门|跨团队|协同|协调|cross[- ]functional|stakeholder", re.IGNORECASE)),
    ("proficiency", re.compile(r"精通|熟练|掌握|专家|proficient|expert|mastery", re.IGNORECASE)),
    ("evaluative", re.compile(r"完整闭环|完整的|熟悉|扎实|全面|出色|优秀|深入|丰富|成熟|强大|技术视野|产品基本功|complete end[- ]to[- ]end|solid|comprehensive|strong", re.IGNORECASE)),
    ("capability", re.compile(r"能够|擅长|独立负责|独立完成|capable of|skilled at", re.IGNORECASE)),
)


def build_resume_semantic_context(
    output: ResumeTailorOutput,
    context: dict[str, Any],
    *,
    force_all_resume_claims: bool = False,
) -> dict[str, Any]:
    """Build an evidence-only review packet for mandatory and risky claims."""
    document = output.tailored_resume
    claims_to_review: list[dict[str, Any]] = [
        {
            "claim_id": "professional-summary",
            "claim_kind": "resume_summary",
            "location": "professional_summary",
            "text": document.professional_summary.text,
            "achievement_status": document.professional_summary.achievement_status,
            "evidence_ids": document.professional_summary.evidence_ids,
            "risk_signals": _ordered_unique([
                "mandatory_professional_summary",
                *_text_risks(document.professional_summary.text),
            ]),
        }
    ]

    profile_evidence_ids = _ordered_unique(
        identifier
        for strength in output.candidate_role_profile.matched_strengths
        for identifier in strength.evidence_ids
    )
    claims_to_review.append(
        {
            "claim_id": "candidate-fit-summary",
            "claim_kind": "candidate_assessment",
            "location": "candidate_role_profile.fit_summary",
            "text": output.candidate_role_profile.fit_summary,
            "achievement_status": "assessment",
            "evidence_ids": profile_evidence_ids,
            "risk_signals": _ordered_unique([
                "mandatory_fit_summary",
                "gap_consistency",
                *_text_risks(output.candidate_role_profile.fit_summary),
            ]),
        }
    )
    for index, strength in enumerate(output.candidate_role_profile.matched_strengths):
        claims_to_review.append(
            {
                "claim_id": f"matched-strength-{index}",
                "claim_kind": "candidate_assessment",
                "location": f"candidate_role_profile.matched_strengths / {strength.requirement}",
                "text": strength.strength,
                "achievement_status": "assessment",
                "evidence_ids": strength.evidence_ids,
                "risk_signals": _ordered_unique([
                    "mandatory_matched_strength",
                    "jd_contamination",
                    *_text_risks(strength.strength),
                ]),
            }
        )
    for index, positioning in enumerate(output.candidate_role_profile.positioning):
        claims_to_review.append(
            {
                "claim_id": f"positioning-{index}",
                "claim_kind": "candidate_assessment",
                "location": "candidate_role_profile.positioning",
                "text": positioning,
                "achievement_status": "assessment",
                "evidence_ids": profile_evidence_ids,
                "risk_signals": _ordered_unique([
                    "mandatory_positioning",
                    "gap_consistency",
                    *_text_risks(positioning),
                ]),
            }
    )

    for section_index, section in enumerate(document.sections):
        is_project_section = _is_project_section(section.title)
        for entry_index, entry in enumerate(section.entries):
            is_skill_entry = _is_skill_entry(section.title, entry.heading)
            for bullet_index, bullet in enumerate(entry.bullets):
                risks = _text_risks(bullet.text)
                if is_skill_entry:
                    risks.append("mandatory_skill_claim")
                if bullet.achievement_status in {"designed", "planned", "observed"}:
                    risks.append(f"achievement_status_{bullet.achievement_status}")
                if force_all_resume_claims:
                    risks.append("forced_post_repair_review")
                if not force_all_resume_claims and not is_skill_entry and not is_project_section:
                    continue
                if not risks:
                    continue
                claims_to_review.append(
                    {
                        "claim_id": f"section-{section_index}-entry-{entry_index}-bullet-{bullet_index}",
                        "claim_kind": "resume_bullet",
                        "location": f"{section.title} / {entry.heading}",
                        "text": bullet.text,
                        "achievement_status": bullet.achievement_status,
                        "evidence_ids": bullet.evidence_ids,
                        "risk_signals": _ordered_unique(risks),
                    }
                )

    cited_ids = {
        identifier for claim in claims_to_review for identifier in claim["evidence_ids"]
    }
    evidence = context.get("candidate_evidence") or {}
    excerpts = [
        item
        for group in ("resume", "portfolio")
        for item in evidence.get(group, [])
        if item.get("evidence_id") in cited_ids
    ]
    return {
        "claims_to_review": claims_to_review,
        "cited_candidate_evidence": excerpts,
        "candidate_gaps": [
            gap.model_dump() if hasattr(gap, "model_dump") else dict(gap)
            for gap in output.candidate_role_profile.gaps
        ],
        "review_policy": {
            "supported": "The claim's scope, ownership, execution stage, and result are directly supported.",
            "ambiguous": "The available evidence is insufficient to choose supported or unsupported safely.",
            "overstated": "The claim is based on evidence but uses stronger scope, ownership, delivery, result, or proficiency wording.",
            "unsupported": "The claim has no supporting candidate evidence or contradicts the supplied evidence.",
        },
        "rewrite_policy": {
            "personal_project": "Use practice/design/planning wording, not unqualified professional experience.",
            "execution_stage": "Never upgrade designed or planned work to launched, delivered, or implemented.",
            "ownership": "Use independent practice/capability instead of responsible for, owner, or leadership wording unless employment evidence proves it.",
            "consistency": "A suggested rewrite must itself satisfy the same review policy and may not contain the wording it criticizes.",
        },
    }


def _is_skill_entry(section_title: str, entry_heading: str) -> bool:
    section = section_title.lower()
    entry = entry_heading.lower()
    if any(marker in entry for marker in ("荣誉", "奖项", "award", "honor")):
        return False
    return any(marker in entry for marker in ("技能", "能力", "skill", "competenc")) or any(
        marker in section for marker in ("技能", "能力", "skill", "competenc")
    )


def _is_project_section(title: str) -> bool:
    normalized = title.lower()
    return any(
        marker in normalized
        for marker in ("项目", "经历", "研究", "project", "experience", "research")
    )


def _ordered_unique(values) -> list[str]:
    return list(dict.fromkeys(values))


def _text_risks(text: str) -> list[str]:
    return [name for name, pattern in _RISK_PATTERNS if pattern.search(text)]


def verify_resume_semantic_review(
    review: ResumeSemanticReview,
    semantic_context: dict[str, Any],
) -> None:
    expected = {item["claim_id"] for item in semantic_context["claims_to_review"]}
    actual = [item.claim_id for item in review.reviews]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise SemanticVerificationError(
            "semantic verifier did not return exactly one review for every risky claim",
            repairable=False,
        )

    allowed_evidence = {
        item["evidence_id"] for item in semantic_context["cited_candidate_evidence"]
    }
    unknown = sorted(
        {
            identifier
            for item in review.reviews
            for identifier in item.supporting_evidence_ids
            if identifier not in allowed_evidence
        }
    )
    if unknown:
        raise SemanticVerificationError(
            f"semantic verifier cited unknown evidence IDs: {', '.join(unknown)}",
            repairable=False,
        )

    ambiguous = [item for item in review.reviews if item.verdict == "ambiguous"]
    if ambiguous:
        detail = _format_issues(ambiguous)
        raise SemanticVerificationError(
            "resume semantic review needs human clarification: " + detail,
            repairable=False,
        )

    repairable = [
        item for item in review.reviews if item.verdict in {"overstated", "unsupported"}
    ]
    if repairable:
        detail = _format_issues(repairable)
        raise SemanticVerificationError(
            "resume contains semantically unsafe claims: " + detail,
            repairable=True,
        )


def _format_issues(items) -> str:
    return "; ".join(
        f"[{item.claim_id}] {item.verdict}: {item.rationale}"
        + (f" Suggested rewrite: {item.suggested_rewrite}" if item.suggested_rewrite else "")
        for item in items
    )
