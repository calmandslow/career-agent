import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from career_agent.repository import JsonStateRepository
import qwen_answer_coach
from pathlib import Path

from career_agent.answer_exports import write_answer_exports
from career_agent.models import ApplicationState
from career_agent.schemas import AnswerPrepProfile
from recheck_answer_coach import recheck


class AnswerPatchTests(unittest.TestCase):
    def state(self):
        question = "如果团队要求Axure，你如何保证产品方案质量？"
        return ApplicationState(
            current_stage="waiting_for_human_review_interview-answer-coach",
            material_evidence={"resume": [{"evidence_id": "RESUME-001", "text": "使用Figma"}]},
            interview_prep_profile={"questions": [{"question": question}]},
            pending_validation_output={"answers": [{
                "question": question, "evidence_status": "partial",
                "answer_structure": ["方法"], "gaps_to_avoid_inventing": ["工具熟练度"],
                "evidence_to_use": ["RESUME-001"], "missing_information": ["缺少Axure经验"],
                "recommended_answer": "我会练习原型并请同事检查产品方案。"}], "delivery_tips": []},
            validation_issues=["old unsupported time promise"],
        )

    def test_tool_learning_not_business_discovery(self):
        state = self.state()
        self.assertEqual(recheck(state)["status"], "local_validation_passed")
        self.assertEqual(state.validation_issues, [])
        self.assertIsNone(state.answer_prep_profile)
        self.assertIsNotNone(state.pending_validation_output)

    def test_time_promise_requires_semantic_review(self):
        state = self.state()
        state.pending_validation_output["answers"][0]["recommended_answer"] = "我会两周内学会Axure。"
        result = recheck(state, approve=True)
        self.assertEqual(result["status"], "semantic_review_required")
        self.assertIsNone(state.answer_prep_profile)

    def test_pending_export_contains_answers_and_status(self):
        state = self.state()
        with tempfile.TemporaryDirectory() as directory:
            paths = write_answer_exports(directory, state)
            self.assertIn("尚未通过验收", paths["readable"].read_text(encoding="utf-8"))
            self.assertIn("请同事", paths["readable"].read_text(encoding="utf-8"))
            self.assertFalse((Path(directory) / "answer_prep_profile.json").exists())
            self.assertEqual(json.loads(paths["profile"].read_text(encoding="utf-8"))["validation_issues"], state.validation_issues)

    def test_explicit_approval_preserves_answers_and_calls(self):
        state = self.state()
        original = AnswerPrepProfile.model_validate(state.pending_validation_output).model_dump()
        from career_agent.answer_semantic import review_key
        for answer in AnswerPrepProfile.model_validate(original).answers:
            state.record("answer_semantic_passed", answer_hash=review_key(answer, state))
        calls = copy.deepcopy(state.llm_calls)
        self.assertEqual(recheck(state, approve=True)["api_calls"], 0)
        self.assertEqual(state.answer_prep_profile, original)
        self.assertEqual(state.llm_calls, calls)
        self.assertIsNone(state.pending_validation_output)
        self.assertEqual(state.current_stage, "completed")

    def test_business_proposal_still_requires_discovery(self):
        state = self.state()
        answer = state.pending_validation_output["answers"][0]
        answer["question"] = "如果迁移到日化场景，你会怎么做？"
        answer["recommended_answer"] = "我会直接设计护肤Agent产品。"
        state.interview_prep_profile["questions"][0]["question"] = answer["question"]
        self.assertEqual(recheck(state, approve=True)["status"], "semantic_review_required")

    def test_repeat_cli_retains_pending_without_constructing_executor(self):
        state = self.state()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = JsonStateRepository(root / "data" / "applications")
            repository.save(state)
            with patch.object(qwen_answer_coach, "PROJECT_ROOT", root), patch("sys.argv", ["qwen_answer_coach.py", state.application_id]), patch.object(qwen_answer_coach, "create_executor") as factory, patch("builtins.print"):
                qwen_answer_coach.main()
            factory.assert_not_called()
            self.assertEqual(repository.load(state.application_id).pending_validation_output, state.pending_validation_output)

    def test_unknown_evidence_prevents_approval(self):
        state = self.state()
        state.pending_validation_output["answers"][0]["evidence_to_use"] = ["FAKE-001"]
        self.assertEqual(recheck(state, approve=True)["status"], "waiting_for_user")
        self.assertIsNone(state.answer_prep_profile)
