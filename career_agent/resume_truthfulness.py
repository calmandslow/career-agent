from __future__ import annotations

from .schemas import ResumeTailorOutput


_CONSERVATIVE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("具备独立负责产品规划的经验", "具备独立产品规划实践"),
    ("独立负责产品规划的经验", "独立产品规划实践"),
    ("独立负责产品规划", "独立开展产品规划实践"),
    ("AI Agent设计经验", "AI Agent设计实践"),
    ("AI Agent 设计经验", "AI Agent 设计实践"),
    ("产品落地经验", "产品方案设计实践"),
    ("产品Owner意识", "独立推进意识"),
    ("Owner意识", "独立推进意识"),
)


CONSERVATIVE_REPAIR_INSTRUCTION = """
Use conservative evidence framing for personal or design-stage projects.
- Treat verifier suggestions as feedback, not automatically trusted final copy.
- Never turn personal project practice into employment or enterprise delivery experience.
- Prefer 实践/设计/规划/验证方案 over 经验/负责/落地/Owner when execution evidence is absent.
- Preserve designed/planned/completed distinctions.
- Do not use 独立负责产品规划的经验, AI Agent设计经验, 产品落地经验, or Owner意识 for design-stage personal work.
- Keep the resume text, candidate profile, resume_evidence, and claim_audit mutually consistent.
""".strip()


def apply_conservative_resume_framing(output: ResumeTailorOutput) -> ResumeTailorOutput:
    """Deterministically weaken known unsafe wording when work experience is absent."""
    if not _lacks_professional_delivery_evidence(output):
        return output

    repaired = output.model_copy(deep=True)
    replacements: dict[str, str] = {}

    def rewrite(text: str) -> str:
        updated = text
        for source, target in _CONSERVATIVE_REPLACEMENTS:
            updated = updated.replace(source, target)
        if updated != text:
            replacements[text] = updated
        return updated

    repaired.tailored_resume.professional_summary.text = rewrite(
        repaired.tailored_resume.professional_summary.text
    )
    for section in repaired.tailored_resume.sections:
        for entry in section.entries:
            for bullet in entry.bullets:
                if bullet.achievement_status in {"designed", "planned", "observed"}:
                    bullet.text = rewrite(bullet.text)

    profile = repaired.candidate_role_profile
    profile.fit_summary = rewrite(profile.fit_summary)
    for strength in profile.matched_strengths:
        strength.strength = rewrite(strength.strength)
    profile.positioning = [rewrite(item) for item in profile.positioning]

    for item in repaired.resume_evidence:
        item.resume_statement = replacements.get(item.resume_statement, rewrite(item.resume_statement))
    for audit in repaired.claim_audit:
        audit.claim = replacements.get(audit.claim, rewrite(audit.claim))

    if replacements:
        repaired.change_notes.append(
            "Harness applied conservative wording for personal/design-stage work before final semantic review."
        )
    return repaired


def _lacks_professional_delivery_evidence(output: ResumeTailorOutput) -> bool:
    gaps = " ".join(
        f"{item.requirement} {item.gap} {item.risk}"
        for item in output.candidate_role_profile.gaps
    )
    markers = (
        "缺乏实际企业",
        "缺乏企业",
        "缺乏实际工作",
        "缺乏工作经验",
        "未提供真实工作经验",
        "personal project",
        "no professional experience",
    )
    lowered = gaps.lower()
    return any(marker.lower() in lowered for marker in markers)
