import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from career_agent import ApplicationState, Intent, JsonStateRepository, Orchestrator, make_plan
from career_agent.executor import OpenAIResponsesExecutor, OpenAICompatibleChatExecutor, QwenExecutor
from career_agent.executor_factory import create_executor
from career_agent.evidence_builder import build_material_evidence
from career_agent.resume_renderer import render_resume_markdown
from career_agent.schemas import ResumeTailorOutput
from career_agent.verifier import AnswerVerificationError, VerificationError, verify_skill_output
from career_agent.mock_executor import MockSkillExecutor
from career_agent.material_loader import MaterialLoader
from career_agent.model_router import ModelRouter
from career_agent.schemas import AnswerPrepProfile, CandidateGap, JDProfile, PreparedAnswer, ResumeSemanticReview
from career_agent.semantic_verifier import (
    SemanticVerificationError,
    build_resume_semantic_context,
    verify_resume_semantic_review,
)
from career_agent.skill_loader import SkillLoader
from career_agent.reporting import summarize_calls, write_run_reports
from career_agent.human_review import accept_pending_resume_review
from career_agent.resume_truthfulness import apply_conservative_resume_framing
from career_agent.telemetry import (
    ObservableCalls,
    ProgressReporter,
    is_retryable_error,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, skill, context: dict, output_model: type[BaseModel]) -> BaseModel:
        self.calls.append({"skill": skill.name, "context": context, "model": output_model.__name__})
        return JDProfile(
            company=context.get("company"),
            role_title="AI Product Manager",
            role_summary="Own AI product discovery, delivery, and iteration.",
            seniority="mid-level",
            responsibilities=[
                {"item": "Plan and ship AI products", "priority": "high", "evidence": "Own AI product delivery"}
            ],
            requirements=[
                {
                    "item": "AI product experience",
                    "category": "product",
                    "priority": "must_have",
                    "evidence": "Experience building AI products",
                }
            ],
            keywords=["AI product", "LLM", "cross-functional"],
            evaluation_dimensions=[
                {
                    "dimension": "product delivery",
                    "why_it_matters": "The role owns delivery",
                    "signals": ["shipped products"],
                }
            ],
            hidden_expectations=["Translate model capability into user value"],
            ambiguities=[],
        )


class FakeResponsesAPI:
    def __init__(self, parsed: BaseModel) -> None:
        self.parsed = parsed
        self.kwargs: dict = {}

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return type("Response", (), {"output_parsed": self.parsed})()


class FakeOpenAIClient:
    def __init__(self, parsed: BaseModel) -> None:
        self.responses = FakeResponsesAPI(parsed)


class FakeChatCompletionsAPI:
    def __init__(self, parsed: BaseModel) -> None:
        self.parsed = parsed
        self.kwargs: dict = {}
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.kwargs = kwargs
        self.calls.append(kwargs)
        message = type("Message", (), {"parsed": self.parsed})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeChatAPI:
    def __init__(self, parsed: BaseModel) -> None:
        self.completions = FakeChatCompletionsAPI(parsed)


class FakeChatClient:
    def __init__(self, parsed: BaseModel) -> None:
        self.chat = FakeChatAPI(parsed)


def make_semantic_review(context: dict, verdicts: dict[str, dict] | None = None):
    verdicts = verdicts or {}
    reviews = []
    for claim in context["claims_to_review"]:
        override = verdicts.get(claim["claim_id"], {})
        reviews.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": override.get("verdict", "supported"),
                "supporting_evidence_ids": override.get(
                    "supporting_evidence_ids", claim["evidence_ids"]
                ),
                "rationale": override.get("rationale", "测试证据支持该表述。"),
                "suggested_rewrite": override.get("suggested_rewrite"),
            }
        )
    return ResumeSemanticReview.model_validate({"reviews": reviews})


class PlannerTests(unittest.TestCase):
    def test_missing_jd_pauses_immediately(self) -> None:
        state = ApplicationState()
        plan = make_plan(Intent.GENERATE_INTERVIEW_QUESTIONS, state)
        self.assertEqual(plan[0].to_dict()["field"], "jd")

    def test_missing_resume_keeps_dependency_then_pauses(self) -> None:
        state = ApplicationState(jd="JD text")
        plan = [step.to_dict() for step in make_plan(Intent.GENERATE_INTERVIEW_QUESTIONS, state)]
        self.assertEqual(plan[0]["name"], "jd-analyzer")
        self.assertEqual(plan[1]["field"], "base_resume")

    def test_complete_inputs_create_expected_pipeline(self) -> None:
        state = ApplicationState(jd="JD text", base_resume="Resume text")
        names = [step.name for step in make_plan(Intent.GENERATE_INTERVIEW_QUESTIONS, state)]
        self.assertEqual(
            names,
            ["jd-analyzer", "resume-tailor", "interview-research", "interview-question-generator"],
        )

    def test_completed_outputs_are_not_repeated(self) -> None:
        state = ApplicationState(
            jd="JD text",
            base_resume="Resume text",
            jd_profile={"role": "PM"},
            tailored_resume="Tailored",
            candidate_role_profile={"fit": []},
            interview_research_profile={"sources": []},
            interview_prep_profile={"questions": []},
        )
        self.assertEqual(make_plan(Intent.GENERATE_INTERVIEW_QUESTIONS, state), [])


class ModelRouterTests(unittest.TestCase):
    def test_routes_fast_and_core_skills(self) -> None:
        router = ModelRouter(fast_model="flash-test", core_model="plus-test")
        self.assertEqual(router.model_for("jd-analyzer"), "flash-test")
        self.assertEqual(router.model_for("resume-claim-verifier"), "flash-test")
        self.assertEqual(router.model_for("resume-tailor"), "plus-test")
        self.assertEqual(router.model_for("interview-question-generator"), "plus-test")

    def test_legacy_qwen_model_overrides_both_tiers(self) -> None:
        with patch.dict(
            "os.environ",
            {"QWEN_MODEL": "one-model", "QWEN_FAST_MODEL": "", "QWEN_CORE_MODEL": ""},
            clear=False,
        ):
            router = ModelRouter.from_qwen_env()
        self.assertEqual(router.fast_model, "one-model")
        self.assertEqual(router.core_model, "one-model")

    def test_qwen_executor_uses_route_for_each_skill(self) -> None:
        parsed = FakeExecutor().execute(
            type("Skill", (), {"name": "jd-analyzer"})(),
            {"jd": "JD", "company": "Example"},
            JDProfile,
        )
        client = FakeChatClient(parsed)
        with patch.dict(
            "os.environ",
            {
                "DASHSCOPE_API_KEY": "test-key",
                "QWEN_MODEL": "",
                "QWEN_FAST_MODEL": "flash-test",
                "QWEN_CORE_MODEL": "plus-test",
            },
            clear=False,
        ):
            executor = QwenExecutor(client=client)
        executor.execute(
            SkillLoader(PROJECT_ROOT / "skills").load("jd-analyzer"),
            {"jd": "JD"},
            JDProfile,
        )
        executor.execute(
            SkillLoader(PROJECT_ROOT / "skills").load("resume-tailor"),
            {"candidate_evidence": {}},
            JDProfile,
        )
        self.assertEqual(client.chat.completions.calls[0]["model"], "flash-test")
        self.assertEqual(client.chat.completions.calls[1]["model"], "plus-test")


class ObservabilityTests(unittest.TestCase):
    def test_dns_failure_is_classified_and_retryable(self) -> None:
        APIConnectionError = type("APIConnectionError", (Exception,), {})
        tracker = ObservableCalls()
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                try:
                    raise socket.gaierror(11001, "getaddrinfo failed")
                except socket.gaierror as cause:
                    raise APIConnectionError("connection error") from cause
            return type("Response", (), {"usage": None})()

        with patch.dict(
            "os.environ",
            {"LLM_MAX_RETRIES": "1", "LLM_RETRY_BASE_SECONDS": "0"},
        ):
            tracker._call_with_retry(
                operation,
                skill_name="jd-analyzer",
                call_type="skill",
                model="test-model",
            )

        records = tracker.drain_call_records()
        self.assertEqual(calls, 2)
        self.assertEqual(records[0]["error_category"], "dns")
        self.assertTrue(records[0]["retryable"])

    def test_quota_exhaustion_is_clear_and_not_retryable(self) -> None:
        class PermissionDeniedError(Exception):
            status_code = 403
            body = {
                "error": {
                    "code": "AllocationQuota.FreeTierOnly",
                    "message": "Free quota exhausted.",
                }
            }

        stream = io.StringIO()
        tracker = ObservableCalls(
            progress=ProgressReporter(
                enabled=True,
                stream=stream,
                heartbeat_seconds=0,
            )
        )
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise PermissionDeniedError("Free quota exhausted")

        with patch.dict(
            "os.environ",
            {"LLM_MAX_RETRIES": "3", "LLM_RETRY_BASE_SECONDS": "0"},
        ):
            with self.assertRaises(PermissionDeniedError):
                tracker._call_with_retry(
                    operation,
                    skill_name="resume-tailor",
                    call_type="skill",
                    model="qwen3.7-plus",
                )

        record = tracker.drain_call_records()[0]
        self.assertEqual(calls, 1)
        self.assertEqual(record["error_category"], "quota_exhausted")
        self.assertFalse(record["retryable"])
        self.assertIn("额度已耗尽", stream.getvalue())
        self.assertIn("不会自动重试", stream.getvalue())

    def test_second_run_resumes_from_failed_skill_without_repeating_completed_skill(self) -> None:
        APIConnectionError = type("APIConnectionError", (Exception,), {})

        class FirstRunExecutor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, skill, context, output_model):
                self.calls.append(skill.name)
                if skill.name == "jd-analyzer":
                    return FakeExecutor().execute(skill, context, output_model)
                raise APIConnectionError("temporary connection failure")

        resume_output, _ = EvidenceBuilderTests._minimal_resume_output(
            summary="应用统计背景。",
            language="zh-CN",
        )

        class SecondRunExecutor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, skill, context, output_model):
                self.calls.append(skill.name)
                if skill.name == "resume-claim-verifier":
                    return make_semantic_review(context)
                return resume_output

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(
                jd="负责 AI 产品规划和指标设计。",
                base_resume="应用统计背景，完成产品项目。",
            )
            repository.save(state)
            first = FirstRunExecutor()
            with self.assertRaises(APIConnectionError):
                Orchestrator(
                    repository,
                    SkillLoader(PROJECT_ROOT / "skills"),
                    first,
                ).run(state.application_id, Intent.TAILOR_RESUME)

            retained = repository.load(state.application_id)
            self.assertIsNotNone(retained.jd_profile)
            self.assertIsNone(retained.tailored_resume)
            self.assertEqual(retained.current_stage, "failed_resume-tailor")

            second = SecondRunExecutor()
            result = Orchestrator(
                repository,
                SkillLoader(PROJECT_ROOT / "skills"),
                second,
            ).run(state.application_id, Intent.TAILOR_RESUME)

        self.assertEqual(result.status, "completed")
        self.assertEqual(first.calls, ["jd-analyzer", "resume-tailor"])
        self.assertEqual(second.calls, ["resume-tailor", "resume-claim-verifier"])

    def test_orchestrator_persists_call_records_with_run_id(self) -> None:
        class RecordedFakeExecutor(FakeExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.records = [
                    {
                        "call_id": "call-1",
                        "run_id": None,
                        "skill": "jd-analyzer",
                        "call_type": "skill",
                        "model": "test-model",
                        "attempt": 1,
                        "status": "completed",
                        "started_at": "2026-09-29T00:00:00+00:00",
                        "duration_seconds": 1.0,
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "error_type": None,
                        "status_code": None,
                    }
                ]

            def drain_call_records(self):
                records = self.records
                self.records = []
                return records

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(jd="JD")
            repository.save(state)
            result = Orchestrator(
                repository,
                SkillLoader(PROJECT_ROOT / "skills"),
                RecordedFakeExecutor(),
            ).run(state.application_id, Intent.ANALYZE_JD)
            loaded = repository.load(state.application_id)

        self.assertEqual(len(loaded.llm_calls), 1)
        self.assertEqual(loaded.llm_calls[0]["run_id"], result.run_id)
        self.assertTrue(
            any(event.event == "llm_call_recorded" for event in loaded.execution_history)
        )

    def test_transient_error_retries_and_records_usage(self) -> None:
        APIConnectionError = type("APIConnectionError", (Exception,), {})
        tracker = ObservableCalls()
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise APIConnectionError("temporary")
            usage = type(
                "Usage",
                (),
                {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
            )()
            return type("Response", (), {"usage": usage})()

        with patch.dict(
            "os.environ",
            {"LLM_MAX_RETRIES": "1", "LLM_RETRY_BASE_SECONDS": "0"},
        ):
            tracker._call_with_retry(
                operation,
                skill_name="resume-tailor",
                call_type="skill",
                model="qwen3.7-plus",
            )

        records = tracker.drain_call_records()
        self.assertEqual(calls, 2)
        self.assertEqual([item["status"] for item in records], ["failed", "completed"])
        self.assertEqual(records[-1]["total_tokens"], 1500)
        self.assertEqual(records[-1]["attempt"], 2)

    def test_permission_error_is_not_retried(self) -> None:
        class PermissionDeniedError(Exception):
            status_code = 403

        tracker = ObservableCalls()
        calls = 0

        def operation():
            nonlocal calls
            calls += 1
            raise PermissionDeniedError("quota")

        with patch.dict(
            "os.environ",
            {"LLM_MAX_RETRIES": "3", "LLM_RETRY_BASE_SECONDS": "0"},
        ):
            with self.assertRaises(PermissionDeniedError):
                tracker._call_with_retry(
                    operation,
                    skill_name="resume-tailor",
                    call_type="skill",
                    model="qwen3.7-plus",
                )

        self.assertEqual(calls, 1)
        self.assertFalse(is_retryable_error(PermissionDeniedError("quota")))
        self.assertEqual(len(tracker.drain_call_records()), 1)

    def test_cost_summary_uses_configured_token_prices(self) -> None:
        calls = [
            {
                "model": "qwen3.7-plus",
                "status": "completed",
                "attempt": 1,
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "duration_seconds": 2.0,
            }
        ]
        pricing = {
            "models": {
                "qwen3.7-plus": {
                    "tiers": [
                        {
                            "max_input_tokens": 256000,
                            "input_cny_per_m_tokens": 2.0,
                            "output_cny_per_m_tokens": 8.0,
                        }
                    ]
                }
            }
        }
        summary = summarize_calls(calls, pricing)

        self.assertEqual(summary["total_tokens"], 1500)
        self.assertAlmostEqual(summary["estimated_cost_cny"], 0.006)

    def test_run_reports_write_json_jsonl_and_markdown_without_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = ApplicationState(jd="private JD", base_resume="private resume")
            state.record("skill_started", skill="jd-analyzer")
            state.llm_calls = [
                {
                    "call_id": "call-1",
                    "run_id": "run-1",
                    "skill": "jd-analyzer",
                    "call_type": "skill",
                    "model": "qwen3.7-plus",
                    "attempt": 1,
                    "status": "completed",
                    "started_at": "2026-09-29T00:00:00+00:00",
                    "duration_seconds": 1.0,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "error_type": None,
                    "status_code": None,
                }
            ]
            paths = write_run_reports(
                directory,
                state,
                {
                    "status": "waiting_for_user",
                    "pending_question": {
                        "field": "human_review",
                        "reason": "private resume claim",
                    },
                },
                run_id="run-1",
            )

            self.assertTrue(all(path.exists() for path in paths.values()))
            report = json.loads(paths["run_report"].read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["completed_calls"], 1)
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in paths.values()
            )
            self.assertNotIn("private JD", combined)
            self.assertNotIn("private resume", combined)


class RepositoryTests(unittest.TestCase):
    def test_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(company="Tencent", role="AI PM")
            state.update_materials(jd="JD", base_resume="Resume")
            repository.save(state)
            loaded = repository.load(state.application_id)
            self.assertEqual(loaded.to_dict(), state.to_dict())

    def test_jd_change_invalidates_dependent_outputs(self) -> None:
        state = ApplicationState(
            jd="Old JD",
            jd_profile={"role": "old"},
            tailored_resume="old",
            candidate_role_profile={"fit": []},
            interview_research_profile={"sources": []},
            interview_prep_profile={"questions": []},
        )
        state.update_materials(jd="New JD")
        self.assertIsNone(state.jd_profile)
        self.assertIsNone(state.tailored_resume)
        self.assertIsNone(state.interview_prep_profile)


class MaterialLoaderTests(unittest.TestCase):
    def test_loads_utf8_text_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            text_path = Path(directory) / "resume.txt"
            markdown_path = Path(directory) / "portfolio.md"
            text_path.write_text("应用统计硕士", encoding="utf-8")
            markdown_path.write_text("# Career Agent", encoding="utf-8")
            loader = MaterialLoader()

            self.assertEqual(loader.load(text_path).text, "应用统计硕士")
            self.assertEqual(loader.load(markdown_path).kind, "text")

    def test_uses_image_reader_for_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "jd.png"
            image_path.write_bytes(b"fake image")
            loader = MaterialLoader(image_reader=lambda path: f"OCR:{path.name}")

            result = loader.load(image_path)
            self.assertEqual(result.kind, "image")
            self.assertEqual(result.text, "OCR:jd.png")


class EvidenceBuilderTests(unittest.TestCase):
    def test_long_portfolio_is_capped_and_source_addressable(self) -> None:
        portfolio = "\n\n".join(
            f"第{index}段：AI 产品、用户研究、指标设计和实验验证。" + ("内容" * 300)
            for index in range(100)
        )
        result = build_material_evidence(
            base_resume="应用统计硕士，完成 AI 产品项目。",
            portfolio=portfolio,
            jd_profile={"keywords": ["AI 产品", "指标设计"]},
            portfolio_budget=5_000,
        )

        self.assertLessEqual(result["stats"]["portfolio_selected_characters"], 5_000)
        self.assertLess(
            result["stats"]["portfolio_selected_characters"],
            result["stats"]["portfolio_original_characters"],
        )
        self.assertTrue(result["resume"][0]["evidence_id"].startswith("RESUME-"))
        self.assertTrue(result["portfolio"][0]["evidence_id"].startswith("PORTFOLIO-"))

    def test_structured_resume_renders_readable_markdown(self) -> None:
        rendered = render_resume_markdown(
            {
                "language": "zh-CN",
                "candidate_name": "张三",
                "contact_items": [{"text": "test@example.com", "evidence_ids": ["RESUME-001"], "achievement_status": "identity"}],
                "target_role": "AI 产品经理",
                "professional_summary": {"text": "应用统计背景。", "evidence_ids": ["RESUME-001"], "achievement_status": "completed"},
                "sections": [
                    {"title": "项目经历", "entries": [{"heading": "Agent 项目", "evidence_ids": ["RESUME-001"], "bullets": [{"text": "完成 Agent 项目。", "evidence_ids": ["RESUME-001"], "achievement_status": "completed"}]}]}
                ],
            }
        )
        self.assertIn("# 张三", rendered)
        self.assertIn("## 职业概述", rendered)
        self.assertIn("### Agent 项目", rendered)

    def test_renderer_deduplicates_identical_subheading_and_date(self) -> None:
        rendered = render_resume_markdown({
            "language": "zh-CN", "candidate_name": "张三", "contact_items": [],
            "target_role": "AI 产品经理",
            "professional_summary": {"text": "应用统计背景。", "evidence_ids": ["RESUME-001"], "achievement_status": "completed"},
            "sections": [{"title": "教育背景", "entries": [{
                "heading": "中山大学 | 应用统计 | 硕士",
                "subheading": "2024-09 ~ 2026-06",
                "date_or_location": "2024-09 ~ 2026-06",
                "evidence_ids": ["RESUME-001"],
                "bullets": [{"text": "机器学习。", "evidence_ids": ["RESUME-001"], "achievement_status": "completed"}],
            }]}],
        })
        self.assertEqual(rendered.count("2024-09 ~ 2026-06"), 1)

    def test_verifier_rejects_gap_disguised_as_strength(self) -> None:
        output = ResumeTailorOutput.model_validate(
            {
                "tailored_resume": {
                    "language": "zh-CN",
                    "candidate_name": None,
                    "contact_items": [],
                    "target_role": "AI 产品经理",
                    "professional_summary": {"text": "完成项目", "evidence_ids": ["RESUME-001"], "achievement_status": "completed"},
                    "sections": [
                        {"title": "项目经历", "entries": [{"heading": "项目", "evidence_ids": ["RESUME-001"], "bullets": [{"text": "完成项目。", "evidence_ids": ["RESUME-001"], "achievement_status": "completed"}]}]},
                        {"title": "教育", "entries": [{"heading": "教育", "evidence_ids": ["RESUME-001"], "bullets": [{"text": "应用统计专业。", "evidence_ids": ["RESUME-001"], "achievement_status": "identity"}]}]},
                        {"title": "技能", "entries": [{"heading": "技能", "evidence_ids": ["RESUME-001"], "bullets": [{"text": "使用 Python。", "evidence_ids": ["RESUME-001"], "achievement_status": "completed"}]}]},
                    ],
                },
                "candidate_role_profile": {
                    "fit_level": "partial",
                    "fit_summary": "部分匹配",
                    "matched_strengths": [
                        {"requirement": "行业经验", "strength": "未提供行业经验", "evidence_ids": ["RESUME-001"]}
                    ],
                    "gaps": [],
                    "positioning": [],
                },
                "resume_evidence": [{"resume_statement": "完成项目", "evidence_ids": ["RESUME-001"]}],
                "claim_audit": [
                    {"claim": "完成项目", "claim_type": "exact_fact", "achievement_status": "completed", "evidence_ids": ["RESUME-001"], "rationale": "原文"},
                    {"claim": "完成项目。", "claim_type": "reframed_fact", "achievement_status": "completed", "evidence_ids": ["RESUME-001"], "rationale": "改写"},
                    {"claim": "应用统计专业。", "claim_type": "identity", "achievement_status": "identity", "evidence_ids": ["RESUME-001"], "rationale": "原文"},
                    {"claim": "使用 Python。", "claim_type": "exact_fact", "achievement_status": "completed", "evidence_ids": ["RESUME-001"], "rationale": "原文"},
                ],
                "change_notes": ["重排内容"],
            }
        )
        context = {
            "candidate_evidence": {
                "resume": [{"evidence_id": "RESUME-001", "source": "resume", "text": "完成项目"}],
                "portfolio": [],
            }
        }
        with self.assertRaises(VerificationError):
            verify_skill_output("resume-tailor", output, context={**context, "output_language": "zh-CN"})

    def test_verifier_rejects_wrong_resume_language(self) -> None:
        output, context = self._minimal_resume_output(summary="Completed a project.", language="en")
        with self.assertRaisesRegex(VerificationError, "language"):
            verify_skill_output("resume-tailor", output, context={**context, "output_language": "zh-CN"})

    def test_verifier_rejects_unsupported_proficiency_claim(self) -> None:
        output, context = self._minimal_resume_output(summary="精通 Figma。", language="zh-CN")
        with self.assertRaisesRegex(VerificationError, "unsupported"):
            verify_skill_output("resume-tailor", output, context={**context, "output_language": "zh-CN"})

    def test_verifier_accepts_audit_wording_variation_with_same_evidence_and_status(self) -> None:
        output, context = self._minimal_resume_output(summary="完成了一个项目。", language="zh-CN")
        output.claim_audit[0].claim = "完成项目"
        verify_skill_output("resume-tailor", output, context={**context, "output_language": "zh-CN"})

    def test_verifier_rejects_missing_audit_record(self) -> None:
        output, context = self._minimal_resume_output(summary="完成了一个项目。", language="zh-CN")
        output.claim_audit.pop()
        with self.assertRaisesRegex(VerificationError, "missing"):
            verify_skill_output("resume-tailor", output, context={**context, "output_language": "zh-CN"})

    @staticmethod
    def _minimal_resume_output(*, summary: str, language: str):
        evidence_ids = ["RESUME-001"]
        claims = [summary, "完成项目。", "应用统计专业。", "使用 Figma。"]
        output = ResumeTailorOutput.model_validate({
            "tailored_resume": {
                "language": language, "candidate_name": None, "contact_items": [],
                "target_role": "AI 产品经理",
                "professional_summary": {"text": summary, "evidence_ids": evidence_ids, "achievement_status": "completed"},
                "sections": [
                    {"title": "项目", "entries": [{"heading": "项目", "evidence_ids": evidence_ids, "bullets": [{"text": claims[1], "evidence_ids": evidence_ids, "achievement_status": "completed"}]}]},
                    {"title": "教育", "entries": [{"heading": "教育", "evidence_ids": evidence_ids, "bullets": [{"text": claims[2], "evidence_ids": evidence_ids, "achievement_status": "completed"}]}]},
                    {"title": "技能", "entries": [{"heading": "工具", "evidence_ids": evidence_ids, "bullets": [{"text": claims[3], "evidence_ids": evidence_ids, "achievement_status": "completed"}]}]},
                ],
            },
            "candidate_role_profile": {"fit_level": "partial", "fit_summary": "部分匹配", "matched_strengths": [], "gaps": [], "positioning": []},
            "resume_evidence": [{"resume_statement": claim, "evidence_ids": evidence_ids} for claim in claims],
            "claim_audit": [{"claim": claim, "claim_type": "reframed_fact", "achievement_status": "completed", "evidence_ids": evidence_ids, "rationale": "测试"} for claim in claims],
            "change_notes": [],
        })
        context = {"candidate_evidence": {"resume": [{"evidence_id": "RESUME-001", "source": "resume", "text": "测试材料"}], "portfolio": []}}
        return output, context


class SemanticVerifierTests(unittest.TestCase):
    def test_builds_small_review_context_for_high_risk_claim(self) -> None:
        output, context = EvidenceBuilderTests._minimal_resume_output(
            summary="拥有从0到1的AI产品规划与指标设计经验。",
            language="zh-CN",
        )
        output.candidate_role_profile.gaps = [
            CandidateGap(requirement="工作经验", gap="未提供真实工作经验", risk="high")
        ]
        context["candidate_evidence"]["resume"][0]["text"] = "完成 Decision Agent 工作流和指标验证方案设计。"

        semantic_context = build_resume_semantic_context(output, context)

        self.assertIsNotNone(semantic_context)
        self.assertEqual(
            semantic_context["claims_to_review"][0]["claim_id"],
            "professional-summary",
        )
        self.assertIn("experience", semantic_context["claims_to_review"][0]["risk_signals"])
        self.assertEqual(len(semantic_context["cited_candidate_evidence"]), 1)

    def test_summary_skills_and_candidate_assessment_are_always_reviewed(self) -> None:
        output, context = EvidenceBuilderTests._minimal_resume_output(
            summary="应用统计背景。",
            language="zh-CN",
        )
        output.candidate_role_profile = output.candidate_role_profile.__class__.model_validate(
            {
                "fit_level": "partial",
                "fit_summary": "部分匹配。",
                "matched_strengths": [
                    {
                        "requirement": "数据能力",
                        "strength": "具备应用统计背景。",
                        "evidence_ids": ["RESUME-001"],
                    }
                ],
                "gaps": [],
                "positioning": ["突出数据分析背景。"],
            }
        )
        semantic_context = build_resume_semantic_context(output, context)
        identifiers = {claim["claim_id"] for claim in semantic_context["claims_to_review"]}

        self.assertIn("professional-summary", identifiers)
        self.assertIn("candidate-fit-summary", identifiers)
        self.assertIn("matched-strength-0", identifiers)
        self.assertIn("positioning-0", identifiers)
        self.assertIn("section-2-entry-0-bullet-0", identifiers)
        self.assertNotIn("section-1-entry-0-bullet-0", identifiers)

    def test_honors_inside_combined_skills_section_are_not_sent_for_routine_review(self) -> None:
        output, context = EvidenceBuilderTests._minimal_resume_output(
            summary="应用统计背景。",
            language="zh-CN",
        )
        section = output.tailored_resume.sections[2]
        section.title = "技能与荣誉"
        section.entries.append(
            section.entries[0].__class__.model_validate(
                {
                    "heading": "荣誉奖项",
                    "evidence_ids": ["RESUME-001"],
                    "bullets": [
                        {
                            "text": "2025学年学校二等奖学金。",
                            "evidence_ids": ["RESUME-001"],
                            "achievement_status": "completed",
                        }
                    ],
                }
            )
        )
        semantic_context = build_resume_semantic_context(output, context)
        identifiers = {claim["claim_id"] for claim in semantic_context["claims_to_review"]}

        self.assertNotIn("section-2-entry-1-bullet-0", identifiers)

    def test_force_all_reviews_every_resume_bullet_after_semantic_repair(self) -> None:
        output, context = EvidenceBuilderTests._minimal_resume_output(
            summary="应用统计背景。",
            language="zh-CN",
        )
        semantic_context = build_resume_semantic_context(
            output,
            context,
            force_all_resume_claims=True,
        )
        identifiers = {claim["claim_id"] for claim in semantic_context["claims_to_review"]}

        self.assertIn("section-0-entry-0-bullet-0", identifiers)
        self.assertIn("section-1-entry-0-bullet-0", identifiers)
        self.assertIn("section-2-entry-0-bullet-0", identifiers)

    def test_overstated_review_requests_repair(self) -> None:
        output, context = EvidenceBuilderTests._minimal_resume_output(
            summary="拥有从0到1的AI产品规划经验。",
            language="zh-CN",
        )
        semantic_context = build_resume_semantic_context(output, context)
        review = make_semantic_review(
            semantic_context,
            {
                "professional-summary": {
                    "verdict": "overstated",
                    "rationale": "材料只支持设计实践，不能证明产品落地经验。",
                    "suggested_rewrite": "具备AI产品规划实践。",
                }
            },
        )
        with self.assertRaises(SemanticVerificationError) as raised:
            verify_resume_semantic_review(review, semantic_context)
        self.assertTrue(raised.exception.repairable)

    def test_ambiguous_review_requires_human_confirmation(self) -> None:
        output, context = EvidenceBuilderTests._minimal_resume_output(
            summary="拥有AI产品经验。",
            language="zh-CN",
        )
        semantic_context = build_resume_semantic_context(output, context)
        review = make_semantic_review(
            semantic_context,
            {
                "professional-summary": {
                    "verdict": "ambiguous",
                    "rationale": "无法判断项目是否真实上线。",
                }
            },
        )
        with self.assertRaises(SemanticVerificationError) as raised:
            verify_resume_semantic_review(review, semantic_context)
        self.assertFalse(raised.exception.repairable)


class MilestoneTwoTests(unittest.TestCase):
    def test_answer_coach_rejects_unknown_evidence(self) -> None:
        output = AnswerPrepProfile.model_validate({
            "answers": [{
                "question": "请介绍项目。", "evidence_status": "sufficient",
                "answer_structure": ["结论", "证据"], "evidence_to_use": ["FAKE-001"],
                "recommended_answer": "这是一个项目。", "gaps_to_avoid_inventing": [],
            }],
            "delivery_tips": [],
        })
        context = {
            "candidate_evidence": {"resume": [{"evidence_id": "RESUME-001"}], "portfolio": []},
            "interview_prep_profile": {"questions": [{"question": "请介绍项目。"}]},
        }
        with self.assertRaisesRegex(VerificationError, "unknown evidence"):
            verify_skill_output("interview-answer-coach", output, context=context)

    def test_answer_coach_does_not_write_answer_when_evidence_is_insufficient(self) -> None:
        output = AnswerPrepProfile.model_validate({
            "answers": [{
                "question": "请说明企业落地结果。", "evidence_status": "insufficient",
                "answer_structure": ["结论"], "evidence_to_use": ["RESUME-001"],
                "missing_information": ["缺少真实企业落地证据"],
                "recommended_answer": "我完成了企业落地。", "gaps_to_avoid_inventing": ["企业落地"],
            }],
            "delivery_tips": [],
        })
        context = {
            "candidate_evidence": {"resume": [{"evidence_id": "RESUME-001"}], "portfolio": []},
            "interview_prep_profile": {"questions": [{"question": "请说明企业落地结果。"}]},
        }
        with self.assertRaisesRegex(VerificationError, "must not fabricate"):
            verify_skill_output("interview-answer-coach", output, context=context)

    def test_answer_coach_rejects_unsupported_time_promise(self) -> None:
        output = AnswerPrepProfile.model_validate({
            "answers": [{
                "question": "你如何适应新工具？", "evidence_status": "partial",
                "answer_structure": ["方法"], "evidence_to_use": ["RESUME-001"],
                "missing_information": ["没有真实适应周期"],
                "recommended_answer": "我会通过练习，在两周内适应新工具。",
                "gaps_to_avoid_inventing": ["适应周期"],
            }],
            "delivery_tips": [],
        })
        context = {
            "candidate_evidence": {"resume": [{"evidence_id": "RESUME-001", "text": "使用Figma"}]},
            "interview_prep_profile": {"questions": [{"question": "你如何适应新工具？"}]},
        }
        with self.assertRaisesRegex(AnswerVerificationError, "unsupported time promise"):
            verify_skill_output("interview-answer-coach", output, context=context)

    def test_answer_coach_rejects_textbook_abtest_without_operational_controls(self) -> None:
        output = AnswerPrepProfile.model_validate({
            "answers": [{
                "question": "如何设计ABTest？", "evidence_status": "partial",
                "answer_structure": ["假设", "指标"], "evidence_to_use": ["RESUME-001"],
                "missing_information": ["没有真实实验结果"],
                "recommended_answer": "我会提出假设，设置实验组和对照组，并观察核心指标。",
                "gaps_to_avoid_inventing": ["实验结果"],
            }],
            "delivery_tips": [],
        })
        context = {
            "candidate_evidence": {"resume": [{"evidence_id": "RESUME-001", "text": "设计验证方案"}]},
            "interview_prep_profile": {"questions": [{"question": "如何设计ABTest？"}]},
        }
        with self.assertRaisesRegex(AnswerVerificationError, "ABTest design is incomplete"):
            verify_skill_output("interview-answer-coach", output, context=context)

    def test_answer_coach_rejects_scenario_that_jumps_to_solution(self) -> None:
        output = AnswerPrepProfile.model_validate({
            "answers": [{
                "question": "如果迁移到日化场景，你会怎么做？", "evidence_status": "partial",
                "answer_structure": ["方案"], "evidence_to_use": ["RESUME-001"],
                "missing_information": ["缺少真实业务信息"],
                "recommended_answer": "我会设计一个护肤方案Agent，帮助用户完成决策。",
                "gaps_to_avoid_inventing": ["业务结论"],
            }],
            "delivery_tips": [],
        })
        context = {
            "candidate_evidence": {"resume": [{"evidence_id": "RESUME-001", "text": "设计Agent"}]},
            "interview_prep_profile": {"questions": [{"question": "如果迁移到日化场景，你会怎么做？"}]},
        }
        with self.assertRaisesRegex(AnswerVerificationError, "must start from discovery"):
            verify_skill_output("interview-answer-coach", output, context=context)

    def test_orchestrator_repairs_only_failed_answer(self) -> None:
        question_one = "你如何适应新工具？"
        question_two = "请介绍项目。"
        initial = AnswerPrepProfile.model_validate({
            "answers": [
                {
                    "question": question_one, "evidence_status": "partial",
                    "answer_structure": ["方法"], "evidence_to_use": ["RESUME-001"],
                    "missing_information": ["没有真实适应周期"],
                    "recommended_answer": "我会在两周内适应新工具。",
                    "gaps_to_avoid_inventing": ["适应周期"],
                },
                {
                    "question": question_two, "evidence_status": "sufficient",
                    "answer_structure": ["结论"], "evidence_to_use": ["RESUME-001"],
                    "recommended_answer": "我完成了一个有材料支持的项目。",
                    "gaps_to_avoid_inventing": [],
                },
            ],
            "delivery_tips": ["简洁"],
        })

        class OneAnswerRepairExecutor:
            def __init__(self):
                self.output_models = []

            def execute(self, skill, context, output_model):
                self.output_models.append(output_model)
                if output_model is PreparedAnswer:
                    self.assert_focused(context)
                    return PreparedAnswer.model_validate({
                        **context["previous_answer"],
                        "recommended_answer": "我会先用真实任务练习，再根据产出质量迭代。",
                    })
                return initial

            @staticmethod
            def assert_focused(context):
                assert "previous_output" not in context
                assert context["target_question"] == question_one

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(
                jd="人工智能产品经理岗位要求",
                base_resume="使用Figma完成项目",
                jd_profile={"role_title": "AI产品经理"},
                tailored_resume="简历",
                tailored_resume_structured={"target_role": "AI产品经理"},
                candidate_role_profile={"fit_summary": "匹配"},
                resume_claim_audit=[],
                material_evidence={
                    "resume": [{"evidence_id": "RESUME-001", "text": "使用Figma完成项目"}],
                    "portfolio": [], "stats": {},
                },
                interview_research_profile={"findings": ["岗位重视产品能力"]},
                interview_prep_profile={
                    "questions": [{"question": question_one}, {"question": question_two}],
                    "focus_areas": ["产品"], "high_risk_topics": [],
                },
            )
            repository.save(state)
            executor = OneAnswerRepairExecutor()
            result = Orchestrator(
                repository, SkillLoader(PROJECT_ROOT / "skills"), executor
            ).run(state.application_id, Intent.PREPARE_ANSWER)
            saved = repository.load(state.application_id).answer_prep_profile

        self.assertEqual(result.status, "completed")
        self.assertEqual(executor.output_models, [AnswerPrepProfile, PreparedAnswer])
        self.assertEqual(saved["answers"][1], initial.model_dump()["answers"][1])
        self.assertNotIn("两周内", saved["answers"][0]["recommended_answer"])

    def test_conservative_framing_downgrades_personal_project_experience(self) -> None:
        output, _ = EvidenceBuilderTests._minimal_resume_output(
            summary="具备独立负责产品规划的经验与AI Agent设计经验。",
            language="zh-CN",
        )
        output.candidate_role_profile.gaps = [
            CandidateGap(requirement="工作经验", gap="缺乏实际企业工作经验", risk="high")
        ]
        output.candidate_role_profile.fit_summary = "具备独立负责产品规划的经验。"
        output.candidate_role_profile.positioning = ["突出AI Agent设计经验与Owner意识。"]
        original_claim = output.tailored_resume.professional_summary.text
        output.claim_audit[0].claim = original_claim

        repaired = apply_conservative_resume_framing(output)

        self.assertIn("独立产品规划实践", repaired.tailored_resume.professional_summary.text)
        self.assertIn("AI Agent设计实践", repaired.tailored_resume.professional_summary.text)
        self.assertNotIn("负责产品规划的经验", repaired.candidate_role_profile.fit_summary)
        self.assertNotIn("Owner意识", repaired.candidate_role_profile.positioning[0])
        self.assertEqual(
            repaired.claim_audit[0].claim,
            repaired.tailored_resume.professional_summary.text,
        )

    def test_orchestrator_repairs_overstated_resume_claim_once(self) -> None:
        overstated, _ = EvidenceBuilderTests._minimal_resume_output(
            summary="拥有从0到1的AI产品规划与指标设计经验。",
            language="zh-CN",
        )
        overstated.candidate_role_profile.gaps = [
            CandidateGap(requirement="工作经验", gap="未提供真实工作经验", risk="high")
        ]
        repaired, _ = EvidenceBuilderTests._minimal_resume_output(
            summary="具备AI产品规划实践，完成工作流、指标体系与验证方案设计。",
            language="zh-CN",
        )
        class SemanticRepairExecutor:
            def __init__(self):
                self.resume_outputs = [overstated, repaired]
                self.calls = []
                self.semantic_review_count = 0

            def execute(self, skill, context, output_model):
                self.calls.append({"skill": skill.name, "context": context})
                if skill.name == "resume-claim-verifier":
                    self.semantic_review_count += 1
                    if self.semantic_review_count == 1:
                        return make_semantic_review(
                            context,
                            {
                                "professional-summary": {
                                    "verdict": "overstated",
                                    "rationale": "设计阶段项目不能表述为无条件的产品经验。",
                                    "suggested_rewrite": "具备AI产品规划实践，完成工作流、指标体系与验证方案设计。",
                                }
                            },
                        )
                    return make_semantic_review(context)
                return self.resume_outputs.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(
                jd="负责人工智能产品规划、需求分析、指标设计和产品落地。",
                base_resume="完成工作流和指标验证方案设计。",
                jd_profile={"role_title": "AI 产品经理"},
            )
            state.material_evidence = {
                "resume": [
                    {
                        "evidence_id": "RESUME-001",
                        "source": "resume",
                        "text": "完成工作流和指标验证方案设计。",
                    }
                ],
                "portfolio": [],
                "stats": {},
            }
            repository.save(state)
            executor = SemanticRepairExecutor()

            result = Orchestrator(
                repository,
                SkillLoader(PROJECT_ROOT / "skills"),
                executor,
            ).run(state.application_id, Intent.TAILOR_RESUME)
            loaded = repository.load(state.application_id)

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                [call["skill"] for call in executor.calls],
                [
                    "resume-tailor",
                    "resume-claim-verifier",
                    "resume-tailor",
                    "resume-claim-verifier",
                ],
            )
            second_review_context = executor.calls[-1]["context"]
            self.assertTrue(
                all(
                    "forced_post_repair_review" in claim["risk_signals"]
                    for claim in second_review_context["claims_to_review"]
                    if claim["claim_kind"] == "resume_bullet"
                )
            )
            self.assertIn("具备AI产品规划实践", loaded.tailored_resume)
            self.assertTrue(
                any(event.event == "skill_validation_retry" for event in loaded.execution_history)
            )

    def test_orchestrator_pauses_on_ambiguous_semantic_claim(self) -> None:
        output, _ = EvidenceBuilderTests._minimal_resume_output(
            summary="拥有AI产品经验。",
            language="zh-CN",
        )
        class AmbiguousExecutor:
            def __init__(self):
                self.calls = []

            def execute(self, skill, context, output_model):
                self.calls.append(skill.name)
                if skill.name == "resume-claim-verifier":
                    return make_semantic_review(
                        context,
                        {
                            "professional-summary": {
                                "verdict": "ambiguous",
                                "rationale": "无法确认是否真实上线。",
                            }
                        },
                    )
                return output

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(
                jd="负责人工智能产品规划、需求分析、指标设计和产品落地。",
                base_resume="测试材料",
                jd_profile={"role_title": "AI 产品经理"},
            )
            state.material_evidence = {
                "resume": [
                    {"evidence_id": "RESUME-001", "source": "resume", "text": "测试材料"}
                ],
                "portfolio": [],
                "stats": {},
            }
            repository.save(state)
            executor = AmbiguousExecutor()

            result = Orchestrator(
                repository,
                SkillLoader(PROJECT_ROOT / "skills"),
                executor,
            ).run(state.application_id, Intent.TAILOR_RESUME)

            self.assertEqual(result.status, "waiting_for_user")
            self.assertIsNotNone(result.started_at)
            self.assertEqual(executor.calls, ["resume-tailor", "resume-claim-verifier"])
            self.assertEqual(len(repository.load(state.application_id).validation_issues), 1)

    def test_human_review_accepts_pending_resume_without_model_call(self) -> None:
        output, _ = EvidenceBuilderTests._minimal_resume_output(
            summary="具备AI产品规划实践。",
            language="zh-CN",
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(
                jd="负责人工智能产品规划。",
                base_resume="测试材料",
                jd_profile={"role_title": "AI 产品经理"},
                material_evidence={
                    "resume": [
                        {"evidence_id": "RESUME-001", "source": "resume", "text": "测试材料"}
                    ],
                    "portfolio": [],
                    "stats": {},
                },
                current_stage="waiting_for_human_review_resume-tailor",
                pending_validation_output=output.model_dump(),
                validation_issues=["manual check"],
            )

            accept_pending_resume_review(state)
            repository.save(state)
            loaded = repository.load(state.application_id)

            self.assertEqual(loaded.current_stage, "completed")
            self.assertIsNone(loaded.pending_validation_output)
            self.assertIsNone(loaded.validation_issues)
            self.assertIn("具备AI产品规划实践", loaded.tailored_resume)
            self.assertTrue(
                any(event.event == "human_review_approved" for event in loaded.execution_history)
            )

    def test_orchestrator_repairs_validation_failure_once(self) -> None:
        invalid, _ = EvidenceBuilderTests._minimal_resume_output(summary="Completed a project.", language="en")
        repaired, _ = EvidenceBuilderTests._minimal_resume_output(summary="完成了一个项目。", language="zh-CN")

        class RepairingExecutor:
            def __init__(self):
                self.outputs = [invalid, repaired]
                self.contexts = []

            def execute(self, skill, context, output_model):
                self.contexts.append(context)
                if skill.name == "resume-claim-verifier":
                    return make_semantic_review(context)
                return self.outputs.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(jd="负责人工智能产品规划、需求分析、指标设计和产品落地。", base_resume="测试材料", jd_profile={"role_title": "AI 产品经理"})
            state.material_evidence = {"resume": [{"evidence_id": "RESUME-001", "source": "resume", "text": "测试材料"}], "portfolio": [], "stats": {}}
            repository.save(state)
            executor = RepairingExecutor()
            result = Orchestrator(repository, SkillLoader(PROJECT_ROOT / "skills"), executor).run(state.application_id, Intent.TAILOR_RESUME)

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(executor.contexts), 3)
            self.assertIn("verification_feedback", executor.contexts[1])
            self.assertIsNotNone(repository.load(state.application_id).tailored_resume)

    def test_orchestrator_pauses_after_second_validation_failure(self) -> None:
        invalid_one, _ = EvidenceBuilderTests._minimal_resume_output(summary="Completed one.", language="en")
        invalid_two, _ = EvidenceBuilderTests._minimal_resume_output(summary="Completed two.", language="en")

        class AlwaysInvalidExecutor:
            def __init__(self):
                self.outputs = [invalid_one, invalid_two]

            def execute(self, skill, context, output_model):
                return self.outputs.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(jd="负责人工智能产品规划、需求分析、指标设计和产品落地。", base_resume="测试材料", jd_profile={"role_title": "AI 产品经理"})
            state.material_evidence = {"resume": [{"evidence_id": "RESUME-001", "source": "resume", "text": "测试材料"}], "portfolio": [], "stats": {}}
            repository.save(state)
            result = Orchestrator(repository, SkillLoader(PROJECT_ROOT / "skills"), AlwaysInvalidExecutor()).run(state.application_id, Intent.TAILOR_RESUME)
            loaded = repository.load(state.application_id)

            self.assertEqual(result.status, "waiting_for_user")
            self.assertEqual(result.pending_question["field"], "human_review")
            self.assertIsNotNone(loaded.pending_validation_output)
            self.assertEqual(len(loaded.validation_issues), 1)

    def test_skill_loader_reads_jd_analyzer(self) -> None:
        skill = SkillLoader(PROJECT_ROOT / "skills").load("jd-analyzer")
        self.assertEqual(skill.name, "jd-analyzer")
        self.assertIn("do not invent", skill.instructions)

    def test_analyze_jd_runs_end_to_end_and_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(company="Example AI", jd="Build and ship LLM products.")
            repository.save(state)
            fake = FakeExecutor()
            orchestrator = Orchestrator(
                repository,
                SkillLoader(PROJECT_ROOT / "skills"),
                fake,
            )

            result = orchestrator.run(state.application_id, Intent.ANALYZE_JD)
            loaded = repository.load(state.application_id)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.completed_steps, ["jd-analyzer"])
            self.assertEqual(loaded.jd_profile["role_title"], "AI Product Manager")
            self.assertEqual(loaded.current_stage, "completed")
            self.assertEqual(fake.calls[0]["context"], {"jd": state.jd, "company": state.company})

    def test_orchestrator_pauses_without_jd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState()
            repository.save(state)
            result = Orchestrator(
                repository,
                SkillLoader(PROJECT_ROOT / "skills"),
                FakeExecutor(),
            ).run(state.application_id, Intent.ANALYZE_JD)
            self.assertEqual(result.status, "waiting_for_user")
            self.assertEqual(result.pending_question["field"], "jd")

    def test_openai_executor_uses_responses_parse_contract(self) -> None:
        parsed = FakeExecutor().execute(
            type("Skill", (), {"name": "jd-analyzer"})(),
            {"jd": "JD", "company": "Example"},
            JDProfile,
        )
        client = FakeOpenAIClient(parsed)
        executor = OpenAIResponsesExecutor(model="test-model", client=client)
        skill = SkillLoader(PROJECT_ROOT / "skills").load("jd-analyzer")
        result = executor.execute(skill, {"jd": "JD", "company": "Example"}, JDProfile)

        self.assertEqual(result.role_title, "AI Product Manager")
        self.assertEqual(client.responses.kwargs["model"], "test-model")
        self.assertIs(client.responses.kwargs["text_format"], JDProfile)

    def test_openai_compatible_executor_uses_chat_parse_contract(self) -> None:
        parsed = FakeExecutor().execute(
            type("Skill", (), {"name": "jd-analyzer"})(),
            {"jd": "JD", "company": "Example"},
            JDProfile,
        )
        client = FakeChatClient(parsed)
        executor = OpenAICompatibleChatExecutor(
            model="qwen-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            client=client,
        )
        skill = SkillLoader(PROJECT_ROOT / "skills").load("jd-analyzer")
        result = executor.execute(skill, {"jd": "JD", "company": "Example"}, JDProfile)

        kwargs = client.chat.completions.kwargs
        self.assertEqual(result.role_title, "AI Product Manager")
        self.assertEqual(kwargs["model"], "qwen-test")
        self.assertIs(kwargs["response_format"], JDProfile)
        self.assertEqual(kwargs["temperature"], 0.1)

    def test_qwen_provider_requires_api_key(self) -> None:
        # Isolate this test from a developer machine that already has a real key.
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "Missing API key"):
                create_executor("qwen")

    def test_failed_execution_is_recorded_without_sensitive_message(self) -> None:
        class FailingExecutor:
            def execute(self, *args, **kwargs):
                raise RuntimeError("secret provider detail")

        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(jd="JD")
            repository.save(state)
            orchestrator = Orchestrator(
                repository,
                SkillLoader(PROJECT_ROOT / "skills"),
                FailingExecutor(),
            )
            with self.assertRaises(RuntimeError):
                orchestrator.run(state.application_id, Intent.ANALYZE_JD)
            loaded = repository.load(state.application_id)
            self.assertEqual(loaded.current_stage, "failed_jd-analyzer")
            self.assertEqual(loaded.execution_history[-1].event, "skill_failed")
            self.assertNotIn("secret", str(loaded.execution_history[-1].detail))


class FullMockHarnessTests(unittest.TestCase):
    def test_mock_runs_six_step_preparation_pipeline_then_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonStateRepository(directory)
            state = ApplicationState(
                company="Mock Company",
                role="AI 产品经理",
                jd="负责 AI Agent 产品规划、跨团队交付和效果验证。",
                base_resume="应用统计硕士，有 Career Agent 和 PickBetter 项目。",
                portfolio="Career Agent：确定性 Planner + Skills + Verifier。",
            )
            repository.save(state)
            harness = Orchestrator(repository, SkillLoader(PROJECT_ROOT / "skills"), MockSkillExecutor())

            preparation = harness.run(state.application_id, Intent.MOCK_INTERVIEW)
            self.assertEqual(
                preparation.completed_steps,
                ["jd-analyzer", "resume-tailor", "interview-research", "interview-question-generator", "interview-answer-coach", "mock-interview"],
            )
            prepared = repository.load(state.application_id)
            self.assertIsNotNone(prepared.jd_profile)
            self.assertIsNotNone(prepared.tailored_resume)
            self.assertTrue(prepared.tailored_resume.startswith("# AI 产品经理"))
            self.assertIsNotNone(prepared.tailored_resume_structured)
            self.assertIsNotNone(prepared.resume_evidence)
            self.assertIsNotNone(prepared.resume_claim_audit)
            self.assertIsNotNone(prepared.resume_change_notes)
            self.assertIsNotNone(prepared.mock_interview_profile)

            prepared.update_materials(interview_record="回答了项目目标、Planner 取舍和验证方法。")
            repository.save(prepared)
            review = harness.run(state.application_id, Intent.REVIEW_INTERVIEW)
            self.assertEqual(review.completed_steps, ["interview-review"])
            self.assertIsNotNone(repository.load(state.application_id).interview_review_profile)

    def test_review_pauses_for_interview_record(self) -> None:
        plan = make_plan(Intent.REVIEW_INTERVIEW, ApplicationState())
        self.assertEqual(plan[0].field, "interview_record")


if __name__ == "__main__":
    unittest.main()
