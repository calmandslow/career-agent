import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from career_agent.answer_semantic import (AnswerReview, ReviewIssue, hard_check, reviewed_candidate,
    review_answer, review_key, has_passed_review)
from career_agent.schemas import PreparedAnswer
from career_agent.models import ApplicationState
from career_agent.skill_loader import LoadedSkill
from career_agent.model_router import ModelRouter
from career_agent.verifier import VerificationError
from recheck_answer_coach import recheck

class SemanticAnswerTests(unittest.TestCase):
    def answer(self):
        return PreparedAnswer(question="假设双方冲突，你会如何协调？", answer_structure=[],
            evidence_to_use=[], evidence_status="insufficient", missing_information=["没有实操"],
            gaps_to_avoid_inventing=[], recommended_answer="以下是假设思路：我会先澄清业务目标，再比较成本和风险。")

    def test_semantic_path_does_not_reject_incidental_keyword_or_time(self):
        answer = self.answer()
        answer.recommended_answer = "假设实验观察周期为两周内，实际周期需要样本量估计，ABTest仅是选项。"
        hard_check(answer, {})

    def test_hard_check_still_rejects_unknown_evidence(self):
        answer = self.answer(); answer.evidence_to_use = ["FAKE"]
        with self.assertRaises(VerificationError): hard_check(answer, {})

    def test_one_repair_and_second_review_only(self):
        answer = self.answer()
        issue = ReviewIssue(code="vague", quote="比较成本", reason="不具体", suggestion="说明成本构成")
        responses = [AnswerReview(verdict="revise", issues=[issue]), answer, AnswerReview(verdict="revise", issues=[issue])]
        class Executor:
            calls = []
            def execute(self, skill, context, model):
                self.calls.append(skill.name)
                return responses.pop(0)
        executor = Executor()
        with tempfile.TemporaryDirectory() as d:
            candidate, review = reviewed_candidate(executor, LoadedSkill("interview-answer-coach", ""), answer, {}, Path(d))
            self.assertEqual(review.verdict, "revise")
            self.assertTrue((Path(d)/"candidate_answer.json").exists())
            self.assertTrue((Path(d)/"final_semantic_review.json").exists())
        self.assertEqual(executor.calls, ["answer-semantic-verifier", "interview-answer-coach", "answer-semantic-verifier"])

    def test_human_review_does_not_trigger_repair(self):
        class Executor:
            count = 0
            def execute(self, *args):
                self.count += 1
                return AnswerReview(verdict="human_review")
        executor = Executor()
        with tempfile.TemporaryDirectory() as d:
            _, review = reviewed_candidate(executor, LoadedSkill("interview-answer-coach", ""), self.answer(), {}, Path(d))
        self.assertEqual(executor.count, 1)
        self.assertEqual(review.verdict, "human_review")

    def test_invented_review_quote_is_rejected(self):
        class Executor:
            def execute(self, *args):
                return AnswerReview(verdict="revise", issues=[ReviewIssue(code="x", quote="不存在的原文", reason="x", suggestion="x")])
        with self.assertRaisesRegex(VerificationError, "quote"):
            review_answer(Executor(), self.answer(), {})

    def test_contradictory_pass_is_rejected(self):
        class Executor:
            def execute(self, *args):
                return AnswerReview(verdict="pass", issues=[ReviewIssue(code="x", quote="业务目标", reason="x", suggestion="x")])
        with self.assertRaisesRegex(VerificationError, "contradictory"):
            review_answer(Executor(), self.answer(), {})

    def test_pass_invalidated_by_answer_evidence_or_new_review(self):
        state = ApplicationState(); answer = self.answer()
        key = review_key(answer, state)
        state.record("answer_semantic_passed", answer_hash=key)
        self.assertTrue(has_passed_review(state, answer))
        answer.recommended_answer += "补充"
        self.assertFalse(has_passed_review(state, answer))
        answer = self.answer(); state.material_evidence = {"resume": []}
        self.assertFalse(has_passed_review(state, answer))
        state.material_evidence = None
        state.record("answer_semantic_review", answer_hash=key, verdict="revise")
        self.assertFalse(has_passed_review(state, answer))

    def test_approval_requires_current_semantic_pass(self):
        answer = self.answer()
        state = ApplicationState(current_stage="waiting_for_human_review_interview-answer-coach",
            pending_validation_output={"answers": [answer.model_dump()], "delivery_tips": []})
        self.assertEqual(recheck(state, approve=True)["status"], "semantic_review_required")
        self.assertIsNone(state.answer_prep_profile)
        state.record("answer_semantic_passed", answer_hash=review_key(answer, state))
        self.assertEqual(recheck(state, approve=True)["status"], "completed")

    def test_review_model_has_independent_route(self):
        with patch.dict("os.environ", {"QWEN_MODEL": "plus-test", "QWEN_REVIEW_MODEL": "max-test"}, clear=True):
            router = ModelRouter.from_qwen_env()
            self.assertEqual(router.model_for("answer-semantic-verifier"), "max-test")
            self.assertEqual(router.model_for("interview-answer-coach"), "plus-test")

    def test_real_executor_path_uses_semantic_instead_of_keyword_rules(self):
        from career_agent.orchestrator import Orchestrator
        from career_agent.schemas import AnswerPrepProfile
        from career_agent.telemetry import ProgressReporter
        class Executor:
            answer_semantic_enabled = True
            calls = 0
            def execute(self, skill, context, model):
                self.calls += 1
                return AnswerReview(verdict="pass")
        class Repo:
            def save(self, state): pass
        executor = Executor()
        orchestrator = Orchestrator(Repo(), None, executor, progress=ProgressReporter(enabled=False))
        state = ApplicationState(); answer = self.answer()
        answer.recommended_answer = "如果安排两周内的观察窗口，需要先估计样本量；ABTest只是验证选项。"
        output = AnswerPrepProfile(answers=[answer], delivery_tips=[])
        orchestrator._verify_output("interview-answer-coach", output, {}, state)
        self.assertEqual(executor.calls, 1)
        orchestrator._verify_output("interview-answer-coach", output, {}, state)
        self.assertEqual(executor.calls, 1)
