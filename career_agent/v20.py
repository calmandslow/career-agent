"""V2.0 conversational agent primitives.

The agent keeps routing, plans, confirmations and memory inspectable.  A model may
write a conversational answer, but it never receives permission to mutate state.
All mutations are represented as typed proposals and executed by WebService.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .models import utc_now


class GeneralAgentReply(BaseModel):
    reply: str = Field(min_length=1, max_length=6000)
    suggested_prompts: list[str] = Field(default_factory=list, max_length=3)


class IntentDecision(BaseModel):
    intent: Literal["general_chat", "career_consultation", "update_progress"]
    scope: Literal["general", "current_job"]
    is_state_mutation: bool
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)


MODES = {"assistant", "focused_training", "mock_interview", "general_chat"}
MUTATING_PROPOSALS = {"progress_update", "run_action", "start_mock_interview", "save_evidence", "complete_training"}

TOPICS = {
    "数据归因": {
        "terms": ["归因", "实验", "对照组", "数据", "增长"],
        "questions": [
            "你如何证明结果来自你的方案，而不是同期活动、季节变化或其他改动？",
            "实验组和对照组如何分流？两个组是否受到相同的外部因素影响？",
            "实验持续多久、覆盖多少用户？你如何判断结果不是短期波动？",
        ],
    },
    "个人贡献": {
        "terms": ["贡献", "负责", "主导", "协作", "职责"],
        "questions": [
            "请把团队结果拆开：哪些决策和交付由你本人负责？",
            "如果没有你，这个项目最可能在哪个环节产生不同结果？",
            "数据、研发和运营分别做了什么，你如何推动他们完成目标？",
        ],
    },
    "方案取舍": {
        "terms": ["取舍", "方案", "决策", "成本", "优先级"],
        "questions": [
            "当时有哪些备选方案？你最终选择这个方案的判断依据是什么？",
            "这个方案牺牲了什么？你如何控制它带来的风险？",
            "如果资源减半，你会保留哪一部分，为什么？",
        ],
    },
}

CONCEPTS = {
    "归因": {"实验", "对照", "随机", "显著性", "口径", "转化", "增长"},
    "贡献": {"负责", "主导", "推动", "协作", "决策", "交付"},
    "增长": {"转化", "留存", "活跃", "漏斗", "会员", "用户"},
    "取舍": {"方案", "成本", "风险", "优先级", "资源", "决策"},
}


def default_thread(application_id: str | None) -> dict[str, Any]:
    return {
        "version": 2,
        "conversation_id": "conv_" + uuid4().hex[:12],
        "application_id": application_id,
        "mode": "assistant",
        "messages": [],
        "pending_proposal": None,
        "session_memory": {},
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def normalize_thread(value: dict[str, Any] | None, application_id: str | None) -> dict[str, Any]:
    thread = default_thread(application_id)
    thread.update(value or {})
    thread["application_id"] = application_id
    thread["mode"] = thread.get("mode") if thread.get("mode") in MODES else "assistant"
    thread["messages"] = list(thread.get("messages") or [])[-200:]
    thread["session_memory"] = dict(thread.get("session_memory") or {})
    return thread


def message(role: str, content: str, **extra: Any) -> dict[str, Any]:
    return {
        "message_id": "msg_" + uuid4().hex[:12],
        "role": role,
        "content": content.strip(),
        "created_at": utc_now(),
        **extra,
    }


def proposal(kind: str, summary: str, payload: dict[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]:
    if kind not in MUTATING_PROPOSALS:
        raise ValueError("Agent proposal type is not allowed")
    return {
        "proposal_id": "prop_" + uuid4().hex[:12],
        "kind": kind,
        "summary": summary,
        "payload": payload,
        "plan": plan,
        "status": "pending",
        "requires_confirmation": True,
        "created_at": utc_now(),
    }


def route_explicit_intent(text: str, *, mode: str = "assistant") -> str | None:
    """Route explicit commands only; ambiguous language belongs to the semantic router."""
    compact = re.sub(r"\s+", "", text.lower())
    if mode == "focused_training":
        if any(word in compact for word in ("结束训练", "结束专项", "退出训练", "完成训练")):
            return "finish_focused_training"
        return "focused_training_answer"
    if re.search(r"(?:补充|新增|记录|保存).{0,6}(?:证据|经历|事实)", compact):
        return "save_evidence"
    if any(word in compact for word in ("专项训练", "专项练习", "专门练", "重点练")):
        return "start_focused_training"
    if any(word in compact for word in ("模拟面试", "模拟一轮", "真实面试来", "完整模拟")):
        return "start_mock_interview"
    if any(word in compact for word in ("准备度", "准备得怎么样", "准备情况", "当前风险")):
        return "show_readiness"
    if any(word in compact for word in ("分析jd", "分析岗位", "岗位分析")):
        return "analyze_jd"
    if any(word in compact for word in ("生成简历", "定制简历", "更新简历", "改简历")):
        return "tailor_resume"
    if any(word in compact for word in ("生成面试题", "准备面试题", "问题清单")):
        return "generate_questions"
    if any(word in compact for word in ("生成准备答案", "准备面试答案", "生成答案")):
        return "prepare_answers"
    return None


def fallback_intent(text: str) -> str:
    """Conservative fallback used only if semantic routing is unavailable."""
    compact = re.sub(r"\s+", "", text.lower())
    hypothetical = any(word in compact for word in ("如果", "假如", "怎么回答", "怎么回应", "如何回答", "如何回应", "会问"))
    progress_event = any(word in compact for word in ("我通过了", "我过了", "已经通过", "收到反馈", "面试官觉得", "面试官认为", "下周参加"))
    if progress_event and not hypothetical:
        return "update_progress"
    career_words = ("求职", "简历", "面试", "岗位", "jd", "职业", "offer", "招聘", "项目经历")
    return "career_consultation" if any(word in compact for word in career_words) else "general_chat"


def reconcile_semantic_intent(text: str, intent: str, *, has_current_job: bool) -> str:
    """Prevent a classifier miss from dropping explicit current-job references."""
    if not has_current_job or intent != "general_chat":
        return intent
    compact = re.sub(r"\s+", "", text.lower())
    personal_reference = re.search(
        r"(?:我的|我有|我该|我应该|帮我|当前|这家|本轮|这一轮).{0,20}"
        r"(?:经历|经验|简历|岗位|公司|面试|[一二三四五六七八九十\d]+面|回答|求职|职业)",
        compact,
    )
    interview_reference = re.search(
        r"(?:业务|技术|hr|终|[一二三四五六七八九十\d]+)面.{0,16}(?:要求|准备|回答|问题|考察)",
        compact,
    )
    return "career_consultation" if personal_reference or interview_reference else intent


def route_intent(text: str, *, mode: str = "assistant") -> str:
    """Backward-compatible deterministic route used by callers without an LLM."""
    return route_explicit_intent(text, mode=mode) or fallback_intent(text)


def select_topic(text: str, risks: list[str] | None = None) -> str:
    source = text + " " + " ".join(risks or [])
    for topic, config in TOPICS.items():
        if topic in source or any(term in source for term in config["terms"]):
            return topic
    return "个人贡献"


def tokens(text: str) -> set[str]:
    parts = set(re.findall(r"[a-z0-9+#./-]+|[\u4e00-\u9fff]{2,}", text.lower()))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        parts.update(chunk[i:i + 2] for i in range(max(0, len(chunk) - 1)))
    return parts


def hybrid_search(items: list[dict[str, Any]], query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Local hybrid retrieval: exact terms plus transparent concept expansion.

    This intentionally avoids pretending that an external embedding store exists.
    The response exposes its scores so it can later be replaced by real embeddings.
    """
    query_tokens = tokens(query)
    expanded = set(query_tokens)
    for concept, related in CONCEPTS.items():
        if concept in query or query_tokens.intersection(related):
            expanded.update(related)
    ranked = []
    for item in items:
        if not item.get("active", True) or item.get("confirmation_status") == "rejected":
            continue
        item_tokens = tokens(item.get("text", "") + " " + item.get("source", ""))
        exact = len(query_tokens.intersection(item_tokens))
        semantic = len(expanded.intersection(item_tokens))
        confidence = 1.0 if item.get("confirmation_status") == "confirmed" else 0.72
        score = (exact * 2.0 + semantic * 0.7) * confidence
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (pair[0], pair[1].get("updated_at", "")), reverse=True)
    return [{**item, "retrieval_score": round(score, 3)} for score, item in ranked[:limit]]


def assess_training_answer(answer: str) -> tuple[int, list[str]]:
    score = min(100, 28 + min(len(answer), 240) // 4)
    gaps = []
    if not re.search(r"\d", answer):
        gaps.append("缺少可以核实的数据、样本或时间信息")
    if not any(word in answer for word in ("我负责", "我主导", "我的", "我推动")):
        gaps.append("个人行动和职责边界还不够明确")
    if not any(word in answer for word in ("因为", "因此", "对照", "所以", "依据", "相比")):
        gaps.append("判断依据和因果链路需要展开")
    return max(0, score - len(gaps) * 8), gaps


def overall_readiness(dimensions: list[dict[str, Any]]) -> int:
    if not dimensions:
        return 0
    weights = {"materials": .2, "job": .2, "resume": .15, "interview": .25, "practice": .2}
    return round(sum(item.get("score", 0) * weights.get(item.get("key"), 0) for item in dimensions))


def stable_manual_evidence_id(text: str) -> str:
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]
    return "ev_" + digest
