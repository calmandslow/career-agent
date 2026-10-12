from __future__ import annotations

from typing import Any


def render_resume_markdown(document: dict[str, Any]) -> str:
    """Render the structured, evidence-aware resume into user-facing Markdown."""
    title = document.get("candidate_name") or document["target_role"]
    lines = [f"# {title}"]
    if document.get("candidate_name"):
        lines.extend(["", f"**{document['target_role']}**"])
    contacts = [item["text"] for item in document.get("contact_items", [])]
    if contacts:
        lines.extend(["", " · ".join(contacts)])
    summary_title = "职业概述" if document.get("language") == "zh-CN" else "Professional Summary"
    lines.extend(["", f"## {summary_title}", document["professional_summary"]["text"]])
    for section in document.get("sections", []):
        lines.extend(["", f"## {section['title']}"])
        for entry in section.get("entries", []):
            heading = entry["heading"]
            subheading = (entry.get("subheading") or "").strip()
            date_or_location = (entry.get("date_or_location") or "").strip()
            if subheading:
                heading += f" | {subheading}"
            if date_or_location and _normalized_label(date_or_location) != _normalized_label(subheading):
                heading += f" | {date_or_location}"
            lines.append(f"### {heading}")
            for item in entry.get("bullets", []):
                lines.append(f"- {item['text']}")
    return "\n".join(lines).strip()


def _normalized_label(value: str) -> str:
    return " ".join(value.split()).lower()


def render_claim_audit_markdown(audit: list[dict[str, Any]], language: str) -> str:
    title = "简历证据审计" if language == "zh-CN" else "Resume Evidence Audit"
    lines = [f"# {title}"]
    for index, item in enumerate(audit, 1):
        lines.extend([
            "", f"## {index}. {item['claim']}",
            f"- Type: {item['claim_type']}",
            f"- Status: {item['achievement_status']}",
            f"- Evidence: {', '.join(item['evidence_ids'])}",
            f"- Rationale: {item['rationale']}",
        ])
    return "\n".join(lines).strip()


def render_semantic_review_markdown(history: list[dict[str, Any]]) -> str:
    lines = ["# 简历高风险表述审查"]
    if not history:
        return "\n".join([*lines, "", "尚无语义审查记录。"])
    for round_number, result in enumerate(history, 1):
        lines.extend(["", f"## 第 {round_number} 轮", f"- Status: {result.get('status', 'unknown')}"])
        if result.get("reason"):
            lines.append(f"- Reason: {result['reason']}")
        for item in result.get("reviews", []):
            lines.extend([
                "",
                f"### {item['claim_id']} · {item['verdict']}",
                f"- Evidence: {', '.join(item.get('supporting_evidence_ids', [])) or 'none'}",
                f"- Rationale: {item['rationale']}",
            ])
            if item.get("suggested_rewrite"):
                lines.append(f"- Suggested rewrite: {item['suggested_rewrite']}")
    return "\n".join(lines).strip()


def render_answer_prep_markdown(profile: dict[str, Any]) -> str:
    lines = ["# 面试答案准备", ""]
    if profile.get("target_role"):
        lines.extend([f"- 目标岗位：{profile['target_role']}", ""])
    for index, answer in enumerate(profile.get("answers", []), 1):
        lines.extend([
            f"## {index}. {answer['question']}", "",
            f"- 优先级：{answer.get('priority', 'P0')}",
            f"- 问题类型：{answer.get('question_type', '')}",
            f"- 回答模式：{answer.get('answer_mode', 'auto')}",
            f"- 考察维度：{answer.get('evaluation_dimension', '')}",
            f"- 证据完整度：{answer.get('evidence_status', '')}",
            f"- 证据：{', '.join(answer.get('evidence_to_use', [])) or '无'}", "",
            "### 回答结构", "", " → ".join(answer.get("answer_structure", [])), "",
            "### 回答骨架", "",
        ])
        lines.extend(f"- {item}" for item in answer.get("answer_skeleton", []))
        lines.extend(["", "### 推荐回答", "", answer.get("recommended_answer") or "[证据不足，暂不生成完整答案]", "", "### 可能追问", ""])
        lines.extend(f"- {item}" for item in answer.get("likely_follow_ups", []))
        lines.extend(["", "### 缺失信息与风险", ""])
        lines.extend(f"- 缺失：{item}" for item in answer.get("missing_information", []))
        lines.extend(f"- 风险：{item}" for item in answer.get("answer_risks", []))
        if answer.get("hypothetical_assumptions"):
            lines.extend(["", "### 假设前提", ""])
            lines.extend(f"- {item}" for item in answer["hypothetical_assumptions"])
        lines.append("")
    lines.extend(["## 表达建议", ""])
    lines.extend(f"- {item}" for item in profile.get("delivery_tips", []))
    return "\n".join(lines).strip() + "\n"
