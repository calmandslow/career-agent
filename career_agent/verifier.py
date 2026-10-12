from __future__ import annotations

import re

from pydantic import BaseModel

from .evidence_builder import evidence_ids
from .answer_policy import infer_answer_mode


class VerificationError(ValueError):
    pass


class AnswerVerificationError(VerificationError):
    """A repairable defect tied to one answer in a batch."""

    def __init__(self, message: str, *, answer_index: int, code: str) -> None:
        super().__init__(message)
        self.answer_index = answer_index
        self.code = code


def verify_skill_output(skill_name: str, output: BaseModel, *, context: dict | None = None) -> None:
    """Lightweight V1 schema/required-content verification."""
    if skill_name == "jd-analyzer":
        data = output.model_dump()
        required_non_empty = ("role_title", "role_summary", "responsibilities", "requirements", "keywords")
        missing = [field for field in required_non_empty if not data.get(field)]
        if missing:
            raise VerificationError(f"jd-analyzer missing required content: {', '.join(missing)}")
        return
    required = {
        "resume-tailor": ("tailored_resume", "candidate_role_profile"),
        "interview-research": ("findings",),
        "interview-question-generator": ("questions", "focus_areas"),
        "interview-answer-coach": ("answers",),
        "mock-interview": ("turns", "overall_feedback"),
        "interview-review": ("summary", "action_items"),
    }
    if skill_name not in required:
        raise VerificationError(f"No verifier registered for skill: {skill_name}")
    data = output.model_dump()
    missing = [field for field in required[skill_name] if not data.get(field)]
    if missing:
        raise VerificationError(f"{skill_name} missing required content: {', '.join(missing)}")
    if skill_name == "interview-research" and data.get("research_mode") not in {
        "jd_only",
        "external_research",
    }:
        raise VerificationError("interview-research has invalid research_mode")
    if skill_name == "resume-tailor":
        allowed = evidence_ids((context or {}).get("candidate_evidence"))
        referenced: list[str] = []
        document = data["tailored_resume"]
        expected_language = (context or {}).get("output_language")
        if expected_language and document["language"] != expected_language:
            raise VerificationError("resume-tailor output language does not follow the JD")
        if not document.get("target_role") or not document.get("professional_summary"):
            raise VerificationError("resume-tailor must produce a complete resume header and summary")
        if len(document.get("sections", [])) < 3:
            raise VerificationError("resume-tailor must produce at least three resume sections")
        if any(not section.get("entries") for section in document["sections"]):
            raise VerificationError("resume-tailor contains an empty resume section")
        if any(not entry.get("bullets") for section in document["sections"] for entry in section["entries"]):
            raise VerificationError("resume-tailor contains an entry without resume bullets")
        required_titles = (
            ("教育", "education"),
            ("项目", "经历", "experience", "project"),
            ("技能", "skill"),
        )
        titles = " ".join(section["title"].lower() for section in document["sections"])
        if any(not any(marker in titles for marker in group) for group in required_titles):
            raise VerificationError("resume-tailor is missing education, experience/project, or skills content")
        for item in data["candidate_role_profile"]["matched_strengths"]:
            referenced.extend(item["evidence_ids"])
            if any(marker in item["strength"].lower() for marker in ("未提供", "未体现", "缺少", "不支持", "没有")):
                raise VerificationError("resume-tailor put a candidate gap inside matched_strengths")
        for item in data.get("resume_evidence", []):
            referenced.extend(item["evidence_ids"])
        for item in document.get("contact_items", []):
            referenced.extend(item["evidence_ids"])
        referenced.extend(document["professional_summary"]["evidence_ids"])
        claims: list[dict] = [*document.get("contact_items", []), document["professional_summary"]]
        for section in document["sections"]:
            for entry in section["entries"]:
                referenced.extend(entry["evidence_ids"])
                for item in entry["bullets"]:
                    referenced.extend(item["evidence_ids"])
                    claims.append(item)
        for item in data.get("claim_audit", []):
            referenced.extend(item["evidence_ids"])
        risky_markers = (
            "顶尖院校", "顶级院校", "精通", "深刻理解", "深刻掌握", "丰富经验", "卓越",
            "全面市场研究", "全面用户研究", "top-tier", "proficient in", "deep understanding",
            "comprehensive market", "comprehensive user", "extensive experience", "expert in",
        )
        if any(marker in claim["text"].lower() for claim in claims for marker in risky_markers):
            raise VerificationError("resume-tailor contains an unsupported evaluative or proficiency claim")
        if any(
            audit["claim_type"] == "identity" and audit["achievement_status"] != "identity"
            for audit in data.get("claim_audit", [])
        ):
            raise VerificationError("resume-tailor uses identity claim_type for a non-identity claim")
        if not _claims_are_audited(claims, data.get("claim_audit", [])):
            raise VerificationError("resume-tailor has resume claims missing from claim_audit")
        bullet_count = sum(len(entry["bullets"]) for section in document["sections"] for entry in section["entries"])
        if bullet_count < 3:
            raise VerificationError("resume-tailor rendered resume is incomplete")
        if not referenced:
            raise VerificationError("resume-tailor must cite candidate evidence")
        unknown = sorted(set(referenced) - allowed)
        if unknown:
            raise VerificationError(f"resume-tailor cited unknown evidence IDs: {', '.join(unknown)}")
    if skill_name == "interview-question-generator" and len(data["questions"]) > 10:
        raise VerificationError("interview-question-generator returned more than 10 questions")
    if skill_name == "interview-answer-coach":
        allowed = evidence_ids((context or {}).get("candidate_evidence"))
        source_questions = {
            item.get("question")
            for item in ((context or {}).get("interview_prep_profile") or {}).get("questions", [])
        }
        evidence_text = _candidate_evidence_text((context or {}).get("candidate_evidence"))
        for answer_index, answer in enumerate(data["answers"]):
            if source_questions and answer["question"] not in source_questions:
                raise VerificationError("interview-answer-coach answered an unknown question")
            unknown = sorted(set(answer["evidence_to_use"]) - allowed)
            if unknown:
                raise VerificationError(
                    "interview-answer-coach cited unknown evidence IDs: " + ", ".join(unknown)
                )
            if (context or {}).get("answer_hard_only"):
                if answer["evidence_status"] != "sufficient" and not answer["missing_information"]:
                    raise VerificationError("Incomplete evidence must list missing information")
                continue
            inferred_mode = infer_answer_mode(answer["question"])
            declared_mode = answer.get("answer_mode", "auto")
            if declared_mode != "auto" and declared_mode != inferred_mode:
                raise AnswerVerificationError("answer mode conflicts with source question; human review required",
                                              answer_index=answer_index, code="answer_mode_conflict")
            text = answer["recommended_answer"]
            if answer["evidence_status"] == "insufficient" and text and inferred_mode == "experience":
                raise VerificationError(
                    "interview-answer-coach must not fabricate an answer when evidence is insufficient"
                )
            if text and inferred_mode in ("hypothetical", "mixed") and (declared_mode != "auto" or answer["evidence_status"] == "insufficient"):
                if not any(x in text for x in ("假设", "假想", "设想", "提案", "尚未验证的方案")):
                    raise AnswerVerificationError("hypothetical answer must be explicitly labeled",
                                                  answer_index=answer_index, code="unlabeled_hypothesis")
                if answer["evidence_status"] == "insufficient" and any(x in text for x in ("我曾", "我当时", "我成功", "我实际完成", "我负责过")):
                    raise AnswerVerificationError("hypothetical answer invents past experience",
                                                  answer_index=answer_index, code="hypothesis_claims_past")
            if answer["evidence_status"] != "sufficient" and not answer["missing_information"]:
                raise AnswerVerificationError(
                    "interview-answer-coach must list missing information for incomplete evidence",
                    answer_index=answer_index,
                    code="missing_information",
                )
            _verify_answer_quality(answer, answer_index, evidence_text)


def _verify_answer_quality(answer: dict, answer_index: int, evidence_text: str) -> None:
    text = answer.get("recommended_answer", "")
    question = answer.get("question", "")
    if not text:
        return

    # Do not turn an unsupported adaptation estimate into a factual promise.
    time_promises = re.findall(
        r"(?:\d+|一|两|三|四|五|六|七|八|九|十|半)[个]?(?:天|周|月)内",
        text,
    )
    unsupported = [promise for promise in time_promises if promise not in evidence_text]
    if unsupported:
        raise AnswerVerificationError(
            f"answer[{answer_index}] contains unsupported time promise: {', '.join(unsupported)}",
            answer_index=answer_index,
            code="unsupported_time_promise",
        )

    combined = f"{question}\n{text}".lower()
    if any(marker in question.lower() for marker in ("abtest", "a/b test", "a/b测试", "a/b 测试")):
        required_groups = {
            "randomization_or_exposure": ("随机", "分流", "实验单元", "曝光"),
            "metrics": ("核心指标", "护栏指标", "guardrail", "指标"),
            "data_quality": ("srm", "样本比例", "污染", "数据质量"),
            "analysis": ("置信区间", "显著性", "实际意义"),
            "sample_size": ("样本量", "mde", "统计功效"),
            "duration_or_stopping": ("实验周期", "观察周期", "实验时长", "停止规则", "停止条件", "提前停止"),
            "rollout_decision": ("上线", "推广", "不发布", "迭代", "不推出"),
            "control_and_treatment": ("对照组", "a组", "a 组", "control"),
        }
        missing = [
            name for name, markers in required_groups.items()
            if not any(marker in text.lower() for marker in markers)
        ]
        if missing:
            raise AnswerVerificationError(
                f"answer[{answer_index}] ABTest design is incomplete: {', '.join(missing)}",
                answer_index=answer_index,
                code="incomplete_ab_test",
            )

    scenario_markers = ("如果", "假设", "场景", "迁移", "如何应用", "怎么应用")
    # Discovery applies to proposing a business/product solution, not every
    # conditional question (tool learning and stakeholder coordination included).
    business_markers = ("日化", "业务场景", "业务目标", "用户痛点", "赋能", "产品方案", "新产品", "产品功能")
    proposal_markers = ("迁移", "挖掘", "设计", "规划", "提出", "怎么做", "如何应用", "怎么应用")
    solution_markers = ("方案", "agent", "功能", "产品")
    discovery_markers = ("先了解", "先验证", "先调研", "用户痛点", "业务目标", "现状", "约束")
    if (
        any(marker in question.lower() for marker in scenario_markers)
        and any(marker in question.lower() for marker in business_markers)
        and any(marker in question.lower() for marker in proposal_markers)
        and any(marker in text.lower() for marker in solution_markers)
        and not any(marker in text.lower() for marker in discovery_markers)
    ):
        raise AnswerVerificationError(
            f"answer[{answer_index}] scenario solution must start from discovery and validation",
            answer_index=answer_index,
            code="scenario_skips_discovery",
        )


def _candidate_evidence_text(candidate_evidence: dict | None) -> str:
    if not candidate_evidence:
        return ""
    chunks: list[str] = []
    for value in candidate_evidence.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                chunks.extend(str(part) for part in item.values() if isinstance(part, (str, int, float)))
    return "\n".join(chunks)


def _claims_are_audited(claims: list[dict], audits: list[dict]) -> bool:
    """Match each resume claim to one audit record without requiring brittle byte equality."""
    if len(audits) != len(claims):
        return False
    unused = set(range(len(audits)))
    for claim in claims:
        exact = next(
            (
                index for index in unused
                if _normalize_claim(audits[index]["claim"]) == _normalize_claim(claim["text"])
            ),
            None,
        )
        if exact is not None:
            unused.remove(exact)
            continue
        structural = next(
            (
                index for index in unused
                if audits[index]["achievement_status"] == claim["achievement_status"]
                and set(audits[index]["evidence_ids"]) == set(claim["evidence_ids"])
            ),
            None,
        )
        if structural is None:
            return False
        unused.remove(structural)
    return True


def _normalize_claim(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()
