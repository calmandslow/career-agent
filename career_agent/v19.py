"""V1.9 career memory, recruitment progress and next-action helpers.

The data in this module is deliberately deterministic. LLM skills may consume the
result, but source/version/confirmation and workflow decisions remain inspectable.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .evidence_builder import build_material_evidence
from .models import utc_now


STAGE_TYPES = {
    "screening", "recruiter", "business", "hiring_manager", "technical",
    "case", "assignment", "panel", "director", "executive", "culture",
    "offer", "custom",
}
STAGE_STATUSES = {"planned", "scheduled", "waiting_result", "passed", "failed", "cancelled"}


def default_process() -> dict[str, Any]:
    return {"status": "active", "current_stage_id": None, "process_source": "partially_known", "stages": []}


def default_meta() -> dict[str, Any]:
    return {
        "material_snapshot": None,
        "jd_sources": [],
        "resume_draft": None,
        "recruitment_process": default_process(),
        "material_impacts": [],
        "progress_notes": [],
    }


def normalize_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    result = default_meta()
    result.update(meta or {})
    process = default_process()
    process.update(result.get("recruitment_process") or {})
    process["stages"] = list(process.get("stages") or [])
    result["recruitment_process"] = process
    result["material_impacts"] = list(result.get("material_impacts") or [])
    result["progress_notes"] = list(result.get("progress_notes") or [])
    return result


def evidence_items(material: dict[str, Any]) -> list[dict[str, Any]]:
    kind = material["kind"]
    if kind not in {"resume", "portfolio"} or not material.get("text", "").strip():
        return []
    built = build_material_evidence(
        base_resume=material["text"] if kind == "resume" else None,
        portfolio=material["text"] if kind == "portfolio" else None,
        jd_profile=None,
        portfolio_budget=1_000_000,
    )
    group = "resume" if kind == "resume" else "portfolio"
    items = []
    for index, item in enumerate(built[group], start=1):
        digest = hashlib.sha256(f'{material["material_id"]}:{index}:{item["text"]}'.encode("utf-8")).hexdigest()[:12]
        items.append({
            "evidence_id": f"ev_{digest}",
            "material_id": material["material_id"],
            "kind": kind,
            "source": material["name"],
            "source_version": material.get("version", 1),
            "text": item["text"],
            "confirmation_status": "pending",
            "active": True,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        })
    return items


def ingest_evidence(store: dict[str, Any] | None, material: dict[str, Any], previous: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    store = {"version": 1, "items": [], "changes": [], **(store or {})}
    items = list(store.get("items") or [])
    for item in items:
        if previous and item.get("material_id") == previous.get("material_id") and item.get("active"):
            item["active"] = False
            item["updated_at"] = utc_now()
    fresh = evidence_items(material)
    previous_texts = {
        item["text"] for item in items
        if previous and item.get("material_id") == previous.get("material_id")
    }
    fresh_texts = {item["text"] for item in fresh}
    added = sorted(fresh_texts - previous_texts)
    removed = sorted(previous_texts - fresh_texts)
    change = {
        "change_id": "change_" + hashlib.sha256(f'{material["material_id"]}:{utc_now()}'.encode()).hexdigest()[:12],
        "kind": material["kind"],
        "previous_material_id": previous.get("material_id") if previous else None,
        "material_id": material["material_id"],
        "source_version": material.get("version", 1),
        "added_count": len(added),
        "removed_count": len(removed),
        "added_preview": added[:5],
        "removed_preview": removed[:5],
        "created_at": utc_now(),
    }
    store["items"] = items + fresh
    store.setdefault("changes", []).append(change)
    return store, change


def search_evidence(store: dict[str, Any] | None, query: str = "", *, include_inactive: bool = False) -> list[dict[str, Any]]:
    items = list((store or {}).get("items") or [])
    if not include_inactive:
        items = [item for item in items if item.get("active")]
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9+#./-]+|[\u4e00-\u9fff]{2,}", query) if term.strip()]
    if terms:
        def score(item: dict[str, Any]) -> int:
            text = (item.get("text", "") + " " + item.get("source", "")).lower()
            return sum(text.count(term) * min(len(term), 12) for term in terms)
        items = [item for item in items if score(item) > 0]
        items.sort(key=lambda item: (score(item), item.get("updated_at", "")), reverse=True)
    else:
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return items


def validate_stage(stage: dict[str, Any]) -> dict[str, Any]:
    result = dict(stage)
    if not str(result.get("name", "")).strip():
        raise ValueError("面试环节名称不能为空。")
    if result.get("stage_type", "custom") not in STAGE_TYPES:
        raise ValueError("面试环节类型无效。")
    if result.get("status", "planned") not in STAGE_STATUSES:
        raise ValueError("面试环节状态无效。")
    result["name"] = str(result["name"]).strip()
    result["focus_areas"] = [str(value).strip() for value in result.get("focus_areas", []) if str(value).strip()]
    result["risks"] = [str(value).strip() for value in result.get("risks", []) if str(value).strip()]
    result["feedback"] = [str(value).strip() for value in result.get("feedback", []) if str(value).strip()]
    return result


def parse_progress_note(text: str, process: dict[str, Any]) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("请先填写求职进展。")
    round_pattern = r"(?:第\s*)?([一二三四五六七八九十\d]+)\s*(?:轮|面)"
    matches = re.findall(round_pattern, text)
    names = [f"{value}面" for value in matches]
    passed_match = re.search(r"(?:通过了?|过了)\s*(?:第\s*)?([一二三四五六七八九十\d]+)\s*(?:轮|面)", text)
    next_match = re.search(
        r"(?:接下来\s*)?(?:准备|参加|进入)\s*"
        r"(业务|技术|专业|HR|hr|终|主管|总监|高管|交叉|小组)?\s*"
        r"(?:第\s*)?([一二三四五六七八九十\d]+)\s*(?:轮|面)", text,
    )
    passed_name = f"{passed_match.group(1)}面" if passed_match else None
    next_name = f"{next_match.group(1) or ''}{next_match.group(2)}面" if next_match else None
    if passed_name and next_name == passed_name and len(names) == 1:
        next_name = None
    feedback = ""
    marker = re.search(r"(?:反馈|觉得|认为|指出|提到)[：,:，]?\s*(.+)$", text)
    if marker:
        feedback = marker.group(1).strip("。 ")
    risks = []
    mappings = {
        "归因": "数据归因解释不充分",
        "数据": "数据口径或结果解释需要补充",
        "贡献": "个人贡献边界不清晰",
        "取舍": "方案取舍说明不足",
        "业务": "业务判断需要进一步验证",
    }
    for keyword, risk in mappings.items():
        if keyword in feedback and risk not in risks:
            risks.append(risk)
    stage_type = "business"
    if next_match and next_match.group(1):
        stage_type = {"技术": "technical", "专业": "technical", "HR": "recruiter", "hr": "recruiter",
                      "主管": "hiring_manager", "总监": "director", "高管": "executive",
                      "交叉": "panel", "小组": "panel"}.get(next_match.group(1), "business")
    schedule_match = re.search(r"((?:本|下)?周[一二三四五六日天]|明天|后天|\d{1,2}\s*月\s*\d{1,2}\s*日)", text)
    return {
        "original_text": text,
        "passed_stage_name": passed_name,
        "next_stage_name": next_name,
        "next_stage_type": stage_type,
        "scheduled_at": None,
        "scheduled_text": schedule_match.group(1).replace(" ", "") if schedule_match else None,
        "feedback": [feedback] if feedback else [],
        "risks": risks,
        "requires_confirmation": True,
        "uncertainties": [value for value in ("下一轮具体时间", "面试官身份", "面试形式") if next_name],
    }


def readiness(state: Any, meta: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    process = normalize_meta(meta)["recruitment_process"]
    current = next((stage for stage in process["stages"] if stage.get("stage_id") == process.get("current_stage_id")), None)
    training = list(meta.get("training_history") or [])
    training_score = max([int(item.get("average_score", 0)) for item in training] or [0])
    dimensions = [
        {"key": "materials", "label": "职业证据", "score": 100 if state.material_evidence else (55 if state.base_resume else 0)},
        {"key": "job", "label": "岗位理解", "score": 100 if state.jd_profile else (40 if state.jd else 0)},
        {"key": "resume", "label": "定制简历", "score": 100 if state.tailored_resume else 0},
        {"key": "interview", "label": "本轮准备", "score": 85 if state.answer_prep_profile else (55 if state.interview_prep_profile else 0)},
        {"key": "practice", "label": "练习验证", "score": 100 if any(item.get("phase") == "completed" for item in sessions) else training_score},
    ]
    risks = list(current.get("risks", [])) if current else []
    impacts = [impact for impact in meta.get("material_impacts", []) if impact.get("status") == "pending"]
    actions = []
    if impacts:
        actions.append({"key": "review_impacts", "priority": "P0", "title": "检查材料更新影响", "reason": f"有 {len(impacts)} 项材料变化可能使已有产物过期。", "target": "resume"})
    elif not state.jd:
        actions.append({"key": "upload_jd", "priority": "P0", "title": "上传岗位 JD", "reason": "缺少岗位信息，暂时无法判断匹配度。", "target": "overview"})
    elif not state.base_resume:
        actions.append({"key": "bind_materials", "priority": "P0", "title": "关联基础材料", "reason": "岗位尚未关联个人经历证据。", "target": "resume"})
    elif not state.jd_profile:
        actions.append({"key": "analyze", "priority": "P0", "title": "分析岗位要求", "reason": "需要先识别岗位的核心要求和评价维度。", "target": "overview"})
    elif risks:
        actions.append({"key": "collect_evidence", "priority": "P0", "title": "补充本轮高风险证据", "reason": risks[0], "target": "prep"})
    elif not state.tailored_resume:
        actions.append({"key": "resume", "priority": "P0", "title": "生成定制简历", "reason": "岗位分析已完成，但还没有可投递简历。", "target": "resume"})
    elif current and not state.interview_prep_profile:
        actions.append({"key": "questions", "priority": "P0", "title": f"准备{current['name']}问题", "reason": "已记录当前招聘环节，但尚未生成针对性问题。", "target": "prep"})
    elif state.interview_prep_profile and not any(item.get("phase") == "completed" for item in sessions):
        actions.append({"key": "practice", "priority": "P0", "title": "完成一轮模拟面试", "reason": "问题已经准备好，需要通过练习验证表达。", "target": "mock"})
    else:
        actions.append({"key": "ready", "priority": "P1", "title": "检查最新进展", "reason": "当前关键准备项已完成，可记录新的招聘进展。", "target": "prep"})
    weights = {"materials": .2, "job": .2, "resume": .15, "interview": .25, "practice": .2}
    overall = round(sum(item["score"] * weights[item["key"]] for item in dimensions))
    explanations = [
        {"key": item["key"], "label": item["label"], "reason": (
            "已有可用于生成的岗位证据" if item["key"] == "materials" and item["score"] else
            "岗位要求已完成结构化分析" if item["key"] == "job" and item["score"] == 100 else
            "已生成并保存当前岗位简历" if item["key"] == "resume" and item["score"] else
            "已结合当前轮次准备问题或答案" if item["key"] == "interview" and item["score"] else
            "已有模拟或专项训练结果" if item["key"] == "practice" and item["score"] else
            "尚未完成"
        )}
        for item in dimensions
    ]
    return {"overall": overall, "dimensions": dimensions, "explanations": explanations,
            "risks": risks, "actions": actions[:3], "current_stage": current,
            "latest_training": training[-1] if training else None}
