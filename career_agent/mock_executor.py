from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from .skill_loader import LoadedSkill


OutputT = TypeVar("OutputT", bound=BaseModel)


class MockSkillExecutor:
    """Deterministic local executor for validating the full Harness without API calls."""

    provider_name = "mock"

    def execute(
        self,
        skill: LoadedSkill,
        context: dict[str, Any],
        output_model: type[OutputT],
    ) -> OutputT:
        fixture = self._fixtures(skill.name, context)
        return output_model.model_validate(fixture)

    @staticmethod
    def _fixtures(name: str, context: dict[str, Any]) -> dict[str, Any]:
        if name == "intent-router":
            text = str(context.get("user_message", "")).lower()
            hypothetical = any(word in text for word in ("如果", "假如", "怎么回答", "怎么回应", "如何回答", "如何回应"))
            progress = any(word in text for word in ("我通过了", "我过了", "已经通过", "收到反馈", "面试官觉得", "面试官认为", "下周参加"))
            career = any(word in text for word in ("求职", "简历", "面试", "岗位", "jd", "职业", "offer", "招聘", "我的经历", "二面"))
            intent = "update_progress" if progress and not hypothetical else ("career_consultation" if career else "general_chat")
            return {
                "intent": intent,
                "scope": "current_job" if intent != "general_chat" and context.get("current_job") else "general",
                "is_state_mutation": intent == "update_progress",
                "confidence": 0.96,
                "reason": "Mock semantic routing decision.",
            }
        if name == "career-agent":
            general = context.get("intent") == "general_chat"
            return {
                "reply": (
                    "这是普通对话，我可以回答；这段内容不会写入职业档案。"
                    if general else
                    "我会基于当前岗位信息回答，但不会在没有确认的情况下修改职业数据。"
                ),
                "suggested_prompts": ["继续说明", "回到当前岗位"],
            }
        if name == "resume-claim-verifier":
            return {
                "reviews": [
                    {
                        "claim_id": item["claim_id"],
                        "verdict": "supported",
                        "supporting_evidence_ids": item["evidence_ids"],
                        "rationale": "Mock verifier treats supplied fixture evidence as supporting.",
                        "suggested_rewrite": None,
                    }
                    for item in context.get("claims_to_review", [])
                ]
            }
        fixtures = {
            "jd-analyzer": {
                "company": context.get("company"),
                "role_title": "AI 产品经理",
                "role_summary": "负责 AI 产品从需求定义到上线验证。",
                "seniority": "未明确",
                "responsibilities": [{"item": "规划并推动 AI 产品落地", "priority": "high", "evidence": "负责产品规划与落地"}],
                "requirements": [{"item": "AI 产品经验", "category": "product", "priority": "must_have", "evidence": "具备 AI 产品经验"}],
                "keywords": ["AI 产品", "LLM", "数据驱动"],
                "evaluation_dimensions": [{"dimension": "产品落地", "why_it_matters": "岗位承担结果责任", "signals": ["完整项目案例"]}],
                "hidden_expectations": ["能把模型能力转化为用户价值"],
                "ambiguities": ["未说明团队规模"],
            },
            "resume-tailor": {
                "tailored_resume": {
                    "language": "zh-CN",
                    "candidate_name": None,
                    "contact_items": [],
                    "target_role": "AI 产品经理｜Agent 产品方向",
                    "professional_summary": {
                        "text": "具备应用统计背景和 AI Agent 项目实践。",
                        "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:2],
                        "achievement_status": "completed",
                    },
                    "sections": [
                        {
                            "title": "项目经历",
                            "entries": [{"heading": "Career Agent", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:2], "bullets": [{"text": "完成 Career Agent 工作流设计与验证。", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:2], "achievement_status": "completed"}]}],
                        },
                        {"title": "教育经历", "entries": [{"heading": "应用统计", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:1], "bullets": [{"text": "具备应用统计教育背景。", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:1], "achievement_status": "identity"}]}]},
                        {"title": "技能", "entries": [{"heading": "产品与数据", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:1], "bullets": [{"text": "使用数据分析方法进行产品验证。", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:1], "achievement_status": "completed"}]}]},
                    ],
                },
                "candidate_role_profile": {
                    "fit_level": "partial",
                    "fit_summary": "具备数据背景和 AI 产品实践，需补强商业化证据。",
                    "matched_strengths": [
                        {
                            "requirement": "数据分析与 AI 产品能力",
                            "strength": "具备应用统计背景和 Career Agent 项目实践",
                            "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:2],
                        }
                    ],
                    "gaps": [
                        {
                            "requirement": "真实业务结果",
                            "gap": "缺少真实上线指标",
                            "risk": "medium",
                        }
                    ],
                    "positioning": ["数据理解较强的 AI 产品经理候选人"],
                },
                "resume_evidence": [
                    {
                        "resume_statement": "突出 AI Agent 项目与数据分析能力",
                        "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:2],
                    }
                ],
                "claim_audit": [
                    {"claim": "具备应用统计背景和 AI Agent 项目实践。", "claim_type": "reframed_fact", "achievement_status": "completed", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:2], "rationale": "由简历与项目证据支持"},
                    {"claim": "完成 Career Agent 工作流设计与验证。", "claim_type": "reframed_fact", "achievement_status": "completed", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:2], "rationale": "由项目证据支持"},
                    {"claim": "具备应用统计教育背景。", "claim_type": "identity", "achievement_status": "identity", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:1], "rationale": "由教育证据支持"},
                    {"claim": "使用数据分析方法进行产品验证。", "claim_type": "reframed_fact", "achievement_status": "completed", "evidence_ids": MockSkillExecutor._available_evidence_ids(context)[:1], "rationale": "由候选人证据支持"},
                ],
                "change_notes": ["重排项目优先级", "加入岗位关键词"],
            },
            "interview-research": {
                "research_mode": "jd_only",
                "company": context.get("company"),
                "role": context.get("role"),
                "findings": [{"finding": "Mock 阶段不访问互联网", "confidence": "high", "source_type": "mock"}],
                "likely_process": ["简历筛选", "业务面试", "综合面试"],
                "limitations": ["尚未接入搜索工具，内容不可作为真实面经"],
            },
            "interview-question-generator": {
                "questions": [{"priority": "P0", "question": "请介绍你的 Career Agent 项目。", "why_likely": "与岗位核心能力直接相关", "evidence_basis": ["候选人项目", "JD 要求"], "follow_ups": ["Planner 为什么不用 LLM？"]}],
                "focus_areas": ["用户价值", "Agent 架构", "验证指标"],
                "high_risk_topics": ["真实上线数据不足"],
            },
            "interview-answer-coach": {
                "target_role": "AI 产品经理",
                "answers": [{"question": "请介绍你的 Career Agent 项目。", "priority": "P0", "question_type": "project_deep_dive", "evaluation_dimension": "AI产品设计与验证", "evidence_status": "partial", "answer_structure": ["结论", "问题", "方案", "取舍", "结果", "反思"], "evidence_to_use": MockSkillExecutor._available_evidence_ids(context)[:2], "missing_information": ["尚无真实用户使用数据"], "answer_skeleton": ["先说明求职流程问题", "再说明Harness与验证"], "recommended_answer": "我先用 Mock 验证工作流，再接入真实模型，并保留真实性审查与失败恢复。当前已完成工程验证，但还没有真实用户数据。", "likely_follow_ups": ["为什么先做Mock？"], "answer_risks": ["不要把工程验证说成用户价值验证"], "gaps_to_avoid_inventing": ["尚无真实用户数据"]}],
                "delivery_tips": ["先讲用户价值，再讲技术结构"],
                "primary_evidence_stories": ["Career Agent"],
                "questions_with_missing_evidence": ["请介绍你的 Career Agent 项目。"],
                "high_risk_follow_ups": ["是否已有真实用户？"],
                "topics_ready_for_mock_interview": ["Career Agent 项目介绍"],
            },
            "mock-interview": {
                "turns": [{"question": "为什么先做 Mock？", "simulated_answer_note": "先验证流程，再验证模型。", "feedback": "补充成本与可替换性的产品取舍。"}],
                "overall_feedback": "结构清楚，需补充量化验证。",
                "next_actions": ["准备一份真实 JD 进行演示"],
            },
            "interview-review": {
                "summary": "Mock 面试记录已完成结构化复盘。",
                "strengths": ["表达结构清楚"],
                "weaknesses": ["量化证据较少"],
                "likely_failure_points": ["项目结果说服力不足"],
                "action_items": ["补充 Completion 与 User Value 指标"],
            },
        }
        if name not in fixtures:
            raise ValueError(f"No mock fixture for skill: {name}")
        return fixtures[name]

    @staticmethod
    def _available_evidence_ids(context: dict[str, Any]) -> list[str]:
        evidence = context.get("candidate_evidence") or {}
        identifiers = [
            item["evidence_id"]
            for group in ("resume", "portfolio")
            for item in evidence.get(group, [])
        ]
        return identifiers or ["RESUME-001"]
