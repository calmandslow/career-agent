import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from career_agent.answer_policy import infer_answer_mode, answer_policy_instructions
from career_agent.answer_update import merge_answer, focused_context
from career_agent.models import ApplicationState
from career_agent.schemas import AnswerPrepProfile, PreparedAnswer
from career_agent.verifier import VerificationError, verify_skill_output


class AnswerModeTests(unittest.TestCase):
    def state(self):
        questions = ["请介绍你过去的团队协调经历。", "假设算法成本太高而业务急需上线，你会如何协调？"]
        answers = [{"question": q, "answer_structure": [], "evidence_to_use": [],
                    "evidence_status": "insufficient", "missing_information": ["无真实协调经历"],
                    "gaps_to_avoid_inventing": ["过去协调结果"], "recommended_answer": ""} for q in questions]
        return ApplicationState(interview_prep_profile={"questions": [{"question": q} for q in questions]},
                                pending_validation_output={"answers": answers, "delivery_tips": []},
                                current_stage="waiting_for_human_review_interview-answer-coach")

    def replacement(self, state, **changes):
        return PreparedAnswer.model_validate({**state.pending_validation_output["answers"][1],
            "answer_mode": "hypothetical", "hypothetical_assumptions": ["业务目标和技术约束待确认"],
            "recommended_answer": "我没有此类实操经验。以下是假设情境下我的处理思路：我会先澄清目标，再讨论缩小范围、分阶段交付与验收。", **changes})

    def test_intents(self):
        self.assertEqual(infer_answer_mode("请介绍你过去的团队协调经历。"), "experience")
        self.assertEqual(infer_answer_mode("假设双方存在冲突，你会如何协调？"), "hypothetical")
        self.assertEqual(infer_answer_mode("在你独立设计的工作流中，你是如何拆解任务？如果迁移到日化业务，你会如何规划？"), "mixed")

    def test_insufficient_hypothetical_can_be_answered(self):
        state = self.state()
        original = copy.deepcopy(state.pending_validation_output["answers"][0])
        result = merge_answer(state, 1, self.replacement(state))
        self.assertEqual(result["current_issues"], [])
        self.assertEqual(state.pending_validation_output["answers"][0], original)
        self.assertIsNone(state.answer_prep_profile)

    def test_unlabeled_hypothesis_is_rejected_without_mutation(self):
        state = self.state()
        previous = copy.deepcopy(state.to_dict())
        with self.assertRaisesRegex(VerificationError, "explicitly labeled"):
            merge_answer(state, 1, self.replacement(state, recommended_answer="我会协调双方。"))
        self.assertEqual(state.to_dict(), previous)

    def test_fabricated_past_in_hypothesis_is_rejected(self):
        state = self.state()
        with self.assertRaisesRegex(VerificationError, "invents past"):
            merge_answer(state, 1, self.replacement(state, recommended_answer="以下是假设方案。我曾成功协调过算法团队。"))

    def test_insufficient_experience_still_cannot_be_fabricated(self):
        state = self.state()
        output = PreparedAnswer.model_validate({**state.pending_validation_output["answers"][0],
            "answer_mode": "experience", "recommended_answer": "我成功交付了业务方案。"})
        with self.assertRaisesRegex(VerificationError, "must not fabricate"):
            merge_answer(state, 0, output)

    def test_mode_cannot_bypass_experience_rules(self):
        state = self.state()
        output = PreparedAnswer.model_validate({**state.pending_validation_output["answers"][0],
            "answer_mode": "hypothetical", "recommended_answer": "假设我已经完成过该项目。"})
        with self.assertRaisesRegex(VerificationError, "conflicts"):
            merge_answer(state, 0, output)

    def test_changed_question_rejected(self):
        state = self.state()
        with self.assertRaisesRegex(ValueError, "preserve question"):
            merge_answer(state, 1, self.replacement(state, question="换一道题"))

    def test_context_contains_only_target_answer(self):
        state = self.state()
        state.material_evidence = {"resume": [{"evidence_id": "OTHER", "text": "Unrelated"}]}
        context = focused_context(state, 1, "生成假设思路")
        self.assertEqual(context["question_intent"], "hypothetical")
        self.assertEqual(context["candidate_evidence"]["resume"], [])
        self.assertNotIn("previous_output", context)

    def test_policy_injected_only_for_answer_coach(self):
        self.assertIn("overrides", answer_policy_instructions("interview-answer-coach"))
        self.assertEqual(answer_policy_instructions("jd-analyzer"), "")

    def test_abtest_requires_duration_and_decision(self):
        question = "如何设计ABTest？"
        answer = PreparedAnswer(question=question, answer_mode="hypothetical", answer_structure=[],
            evidence_to_use=[], evidence_status="partial", missing_information=["无实操"], gaps_to_avoid_inventing=[],
            recommended_answer="以下是假设方案：用户随机分流；对照组现有推荐，实验组AI推荐。曝光后检查核心指标、护栏指标、SRM。按样本量和MDE设计，报告置信区间。")
        with self.assertRaisesRegex(VerificationError, "duration_or_stopping"):
            verify_skill_output("interview-answer-coach", AnswerPrepProfile(answers=[answer], delivery_tips=[]))
        answer.recommended_answer += "提前定义实验周期和停止规则，再根据收益与风险决定上线或迭代。"
        verify_skill_output("interview-answer-coach", AnswerPrepProfile(answers=[answer], delivery_tips=[]))

    def test_single_answer_cli_saves_usage_and_keeps_other_answer(self):
        import qwen_update_answer
        from career_agent.repository import JsonStateRepository
        state = self.state()
        replacement = self.replacement(state)
        class Executor:
            def set_run_id(self, run_id): self.run_id = run_id
            def execute(self, skill, context, model):
                from career_agent.answer_semantic import AnswerReview
                return AnswerReview(verdict="pass") if model is AnswerReview else replacement
            def drain_call_records(self):
                return [{"run_id": self.run_id, "model": "test-model", "attempt": 1,
                         "status": "completed", "input_tokens": 5, "output_tokens": 8,
                         "total_tokens": 13, "duration_seconds": 1}]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repository = JsonStateRepository(root / "data" / "applications")
            repository.save(state)
            with patch.object(qwen_update_answer, "PROJECT_ROOT", root), patch("sys.argv", ["update", state.application_id, "--question", "2", "--feedback", "假设思路"]), patch.object(qwen_update_answer, "create_executor", return_value=Executor()), patch.object(qwen_update_answer, "SkillLoader") as loader, patch("builtins.print"):
                loader.return_value.load.return_value = SimpleNamespace(name="interview-answer-coach")
                qwen_update_answer.main()
            loaded = repository.load(state.application_id)
            self.assertEqual(loaded.pending_validation_output["answers"][0], state.pending_validation_output["answers"][0])
            self.assertEqual(loaded.llm_calls[-1]["total_tokens"], 13)
            logs = list((root / "outputs").rglob("execution_log.jsonl"))
            self.assertEqual(len(logs), 1)
            self.assertNotIn("无真实协调经历", logs[0].read_text(encoding="utf-8"))

    def test_api_failure_retains_answers_and_records_failed_usage(self):
        import qwen_update_answer
        from career_agent.repository import JsonStateRepository
        state = self.state()
        class Executor:
            def set_run_id(self, run_id): self.run_id = run_id
            def execute(self, *args): raise RuntimeError("connection failure")
            def drain_call_records(self):
                return [{"run_id": self.run_id, "model": "test-model", "attempt": 1,
                         "status": "failed", "error_type": "APIConnectionError"}]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repository = JsonStateRepository(root / "data" / "applications")
            repository.save(state)
            with patch.object(qwen_update_answer, "PROJECT_ROOT", root), patch("sys.argv", ["update", state.application_id, "--question", "2", "--feedback", "假设思路"]), patch.object(qwen_update_answer, "create_executor", return_value=Executor()), patch.object(qwen_update_answer, "SkillLoader"), patch("builtins.print"):
                with self.assertRaisesRegex(RuntimeError, "connection failure"):
                    qwen_update_answer.main()
            loaded = repository.load(state.application_id)
            self.assertEqual(loaded.pending_validation_output, state.pending_validation_output)
            self.assertEqual(loaded.llm_calls[-1]["status"], "failed")

    def test_coordination_answer_may_mention_abtest_without_full_design(self):
        state = self.state()
        replacement = self.replacement(state, recommended_answer="以下是假设情境下我的处理思路：我会澄清业务目标，与算法团队比较技术成本，并将ABTest作为后续验证选项。")
        self.assertEqual(merge_answer(state, 1, replacement)["current_issues"], [])

    def test_abtest_prompt_keywords_do_not_supply_missing_answer_controls(self):
        question = "如何设计ABTest？请解释随机分流、曝光、护栏指标、SRM、置信区间、样本量、实验周期、上线决策和对照组。"
        answer = PreparedAnswer(question=question, answer_mode="hypothetical", answer_structure=[], evidence_to_use=[], evidence_status="partial", missing_information=["无实操"], gaps_to_avoid_inventing=[], recommended_answer="以下是假设方案：观察指标。")
        with self.assertRaisesRegex(VerificationError, "randomization_or_exposure"):
            verify_skill_output("interview-answer-coach", AnswerPrepProfile(answers=[answer], delivery_tips=[]))
