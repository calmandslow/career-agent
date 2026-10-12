from __future__ import annotations

import re
from typing import Any


DEFAULT_PORTFOLIO_BUDGET = 24_000
CHUNK_SIZE = 1_200
EVIDENCE_ANCHORS = (
    "问卷",
    "访谈",
    "北极星",
    "指标体系",
    "task completion rate",
    "acceptance rate",
    "correction rate",
    "decision agent",
    "来源追溯",
    "不确定性",
)


def build_material_evidence(
    *,
    base_resume: str | None,
    portfolio: str | None,
    jd_profile: dict[str, Any] | None,
    portfolio_budget: int = DEFAULT_PORTFOLIO_BUDGET,
) -> dict[str, Any]:
    """Create compact, source-addressable evidence for downstream skills.

    The resume is retained in full because it is the candidate's primary record.
    Long portfolio material is split, ranked against the JD, and capped so a model
    does not repeatedly receive the complete source document.
    """
    resume_chunks = _chunk_material(base_resume or "", "RESUME")
    portfolio_chunks = _chunk_material(portfolio or "", "PORTFOLIO")
    terms = _jd_terms(jd_profile or {})

    ranked = sorted(
        portfolio_chunks,
        key=lambda item: (_relevance_score(item["text"], terms), -item["order"]),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    used = 0
    selected_ids: set[str] = set()
    # Preserve at least one concrete excerpt for common project-evidence types.
    for anchor in EVIDENCE_ANCHORS:
        match = next((item for item in ranked if anchor in item["text"].lower()), None)
        if match is None or match["evidence_id"] in selected_ids:
            continue
        if selected and used + len(match["text"]) > portfolio_budget:
            continue
        selected.append(match)
        selected_ids.add(match["evidence_id"])
        used += len(match["text"])
    for item in ranked:
        if item["evidence_id"] in selected_ids:
            continue
        size = len(item["text"])
        if selected and used + size > portfolio_budget:
            continue
        selected.append(item)
        selected_ids.add(item["evidence_id"])
        used += size
        if used >= portfolio_budget:
            break
    selected.sort(key=lambda item: item["order"])

    public_resume = [_public_item(item) for item in resume_chunks]
    public_portfolio = [_public_item(item) for item in selected]
    return {
        "resume": public_resume,
        "portfolio": public_portfolio,
        "stats": {
            "resume_original_characters": len(base_resume or ""),
            "portfolio_original_characters": len(portfolio or ""),
            "portfolio_selected_characters": sum(len(item["text"]) for item in public_portfolio),
            "portfolio_total_chunks": len(portfolio_chunks),
            "portfolio_selected_chunks": len(public_portfolio),
        },
    }


def evidence_ids(evidence: dict[str, Any] | None) -> set[str]:
    if not evidence:
        return set()
    return {
        item["evidence_id"]
        for group in ("resume", "portfolio")
        for item in evidence.get(group, [])
        if item.get("evidence_id")
    }


def _public_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "evidence_id": item["evidence_id"],
        "source": item["source"],
        "text": item["text"],
    }


def _chunk_material(text: str, prefix: str) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    source = prefix.lower()
    chunks: list[dict[str, Any]] = []
    counter = 1
    sections = re.split(r"(?=\[SOURCE:\s*[^\]]+\])", text)
    for section in sections:
        section = section.strip()
        if not section:
            continue
        source_match = re.match(r"\[SOURCE:\s*([^\]]+)\]\s*", section)
        if source_match:
            source = source_match.group(1).strip()
            section = section[source_match.end() :].strip()
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n|(?=\n[•·])", section) if part.strip()]
        buffer = ""
        for paragraph in paragraphs:
            if buffer and len(buffer) + len(paragraph) + 1 > CHUNK_SIZE:
                chunks.append(_chunk(prefix, counter, source, buffer))
                counter += 1
                buffer = ""
            if len(paragraph) > CHUNK_SIZE:
                if buffer:
                    chunks.append(_chunk(prefix, counter, source, buffer))
                    counter += 1
                    buffer = ""
                for start in range(0, len(paragraph), CHUNK_SIZE):
                    chunks.append(_chunk(prefix, counter, source, paragraph[start : start + CHUNK_SIZE]))
                    counter += 1
            else:
                buffer = f"{buffer}\n{paragraph}".strip()
        if buffer:
            chunks.append(_chunk(prefix, counter, source, buffer))
            counter += 1
    return chunks


def _chunk(prefix: str, counter: int, source: str, text: str) -> dict[str, Any]:
    return {
        "evidence_id": f"{prefix}-{counter:03d}",
        "source": source,
        "text": text.strip(),
        "order": counter,
    }


def _jd_terms(profile: dict[str, Any]) -> list[str]:
    values: list[str] = list(profile.get("keywords") or [])
    values.extend(item.get("item", "") for item in profile.get("responsibilities") or [])
    values.extend(item.get("item", "") for item in profile.get("requirements") or [])
    terms: set[str] = set()
    for value in values:
        value = str(value).lower().strip()
        if value:
            terms.add(value)
        terms.update(token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9+.#/-]{1,}", value))
        for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", value):
            for width in (2, 3, 4):
                terms.update(sequence[start : start + width] for start in range(len(sequence) - width + 1))
    # These terms identify concrete candidate evidence rather than generic prose.
    terms.update({"指标", "验证", "问卷", "访谈", "用户研究", "项目", "agent", "a/b test", "abtest"})
    return sorted(terms, key=len, reverse=True)


def _relevance_score(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(min(lowered.count(term), 3) * min(len(term), 12) for term in terms)
