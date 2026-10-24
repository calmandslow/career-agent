import tempfile
import unittest
from pathlib import Path

from career_agent.skill_loader import SkillLoader
from career_agent.mock_executor import MockSkillExecutor
from career_agent.web_service import WebService
from career_agent.v20 import reconcile_semantic_intent


ROOT = Path(__file__).resolve().parents[1]


class V20AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = WebService(self.root, "mock")
        self.service.loader = SkillLoader(ROOT / "skills")

    def tearDown(self):
        self.service.tasks.close()
        self.temp.cleanup()

    def create_job(self):
        return self.service.create_app("目标公司", "增长产品经理")["application_id"]

    def test_general_chat_does_not_mutate_career_state(self):
        app_id = self.create_job()
        before = self.service.repo.load(app_id).to_dict()
        result = self.service.agent_message("什么是机会成本？", app_id)
        self.assertEqual(result["message"]["intent"], "general_chat")
        self.assertEqual(result["thread"]["mode"], "general_chat")
        self.assertIsNone(result["proposal"])
        self.assertIn("不会写入职业档案", result["message"]["content"])
        self.assertEqual(before, self.service.repo.load(app_id).to_dict())

    def test_semantic_router_uses_job_context_for_implicit_career_question(self):
        app_id = self.create_job()
        result = self.service.agent_message(
            "结合我的经历和业务二面要求，我应该怎样回答数据归因问题？", app_id,
        )
        self.assertEqual(result["message"]["intent"], "career_consultation")
        self.assertIsNone(result["proposal"])

    def test_hypothetical_interviewer_question_is_not_progress_update(self):
        app_id = self.create_job()
        result = self.service.agent_message(
            "如果面试官质疑留存提升来自同期活动，我该怎么回应？", app_id,
        )
        self.assertEqual(result["message"]["intent"], "career_consultation")
        self.assertIsNone(result["proposal"])

    def test_current_job_reference_guardrail_corrects_classifier_miss(self):
        self.assertEqual(
            reconcile_semantic_intent(
                "结合我的经历和业务二面要求，我应该怎样回答数据归因问题？",
                "general_chat",
                has_current_job=True,
            ),
            "career_consultation",
        )
        self.assertEqual(
            reconcile_semantic_intent("什么是机会成本？", "general_chat", has_current_job=True),
            "general_chat",
        )

    def test_current_job_guardrail_survives_semantic_router_outage(self):
        def factory(provider, progress):
            class RouterOutageExecutor(MockSkillExecutor):
                def execute(self, skill, context, output_model):
                    if skill.name == "intent-router":
                        raise RuntimeError("semantic router unavailable")
                    return super().execute(skill, context, output_model)

            return RouterOutageExecutor()

        self.service.executor_factory = factory
        app_id = self.create_job()
        result = self.service.agent_message(
            "结合我的经历和业务二面要求，我应该怎样回答数据归因问题？", app_id,
        )
        self.assertEqual(result["message"]["intent"], "career_consultation")
        self.assertIsNone(result["proposal"])

    def test_each_job_has_an_independent_thread_and_visible_context(self):
        first = self.service.create_app("甲公司", "增长产品经理")["application_id"]
        second = self.service.create_app("乙公司", "AI 产品经理")["application_id"]
        self.service.agent_message("什么是机会成本？", first)
        first_thread = self.service.agent_thread(first)
        second_thread = self.service.agent_thread(second)
        self.assertNotEqual(first_thread["conversation_id"], second_thread["conversation_id"])
        self.assertEqual(first_thread["context"]["company"], "甲公司")
        self.assertEqual(second_thread["context"]["company"], "乙公司")
        self.assertTrue(first_thread["messages"])
        self.assertFalse(second_thread["messages"])

    def test_progress_is_previewed_and_requires_confirmation(self):
        app_id = self.create_job()
        result = self.service.agent_message(
            "我通过了一面，下周三参加业务二面。一面主要问增长项目，面试官觉得我的数据归因和个人贡献讲得不够清楚。",
            app_id,
        )
        proposal = result["proposal"]
        self.assertEqual(proposal["kind"], "progress_update")
        self.assertTrue(proposal["requires_confirmation"])
        self.assertEqual(self.service.meta(app_id)["recruitment_process"]["stages"], [])

        confirmed = self.service.confirm_agent_proposal(proposal["proposal_id"], app_id)
        process = confirmed["result"]["web"]["recruitment_process"]
        self.assertEqual([stage["name"] for stage in process["stages"]], ["一面", "业务二面"])
        self.assertEqual(process["stages"][1]["stage_type"], "business")
        self.assertEqual(process["stages"][1]["scheduled_text"], "下周三")
        self.assertIsNone(confirmed["thread"]["pending_proposal"])

    def test_generation_intents_create_typed_tool_plans(self):
        app_id = self.create_job()
        cases = [
            ("分析 JD", "analyze"),
            ("帮我生成定制简历", "resume"),
            ("生成面试题", "questions"),
            ("生成准备答案", "answers"),
        ]
        for text, action in cases:
            result = self.service.agent_message(text, app_id)
            self.assertEqual(result["proposal"]["kind"], "run_action")
            self.assertEqual(result["proposal"]["payload"]["action"], action)
            self.assertTrue(result["message"]["plan"])

    def test_focused_training_is_separate_and_writes_back_after_confirmation(self):
        app_id = self.create_job()
        self.service.save_stage(app_id, {
            "name": "业务二面", "stage_type": "business", "status": "planned",
            "focus_areas": [], "feedback": [], "risks": ["数据归因解释不充分"], "make_current": True,
        })
        started = self.service.agent_message("开始数据归因专项训练", app_id)
        self.assertEqual(started["thread"]["mode"], "focused_training")
        self.assertIn("数据归因专项训练", started["message"]["content"])

        answer = self.service.agent_message(
            "我负责实验设计，因为两个组都参加活动，所以用用户 ID 随机分流，对照比较了 12 万用户两周数据。",
            app_id,
        )
        self.assertEqual(answer["message"]["intent"], "focused_training_answer")
        finished = self.service.agent_message("结束训练", app_id)
        self.assertEqual(finished["proposal"]["kind"], "complete_training")
        self.assertFalse(self.service.meta(app_id).get("training_history"))

        self.service.confirm_agent_proposal(finished["proposal"]["proposal_id"], app_id)
        app = self.service.get_app(app_id)
        self.assertEqual(app["web"]["training_history"][-1]["topic"], "数据归因")
        self.assertEqual(app["dashboard"]["latest_training"]["topic"], "数据归因")
        self.assertEqual(self.service.agent_thread(app_id)["mode"], "assistant")

    def test_manual_evidence_and_mock_interview_are_typed_tools(self):
        app_id = self.create_job()
        evidence = self.service.agent_message(
            "补充证据：我按注册 30 天划分新老用户，新用户次周留存提升 3.2 个百分点。", app_id,
        )
        self.assertEqual(evidence["proposal"]["kind"], "save_evidence")
        saved = self.service.confirm_agent_proposal(evidence["proposal"]["proposal_id"], app_id)
        item = saved["result"]["evidence"]
        self.assertEqual(item["confirmation_status"], "confirmed")
        self.assertIn(item["evidence_id"], [entry["evidence_id"] for entry in self.service.repo.load(app_id).material_evidence["manual"]])

        state = self.service.repo.load(app_id)
        state.interview_prep_profile = {"questions": [{"question": "请介绍增长项目。", "priority": "P0"}]}
        self.service.repo.save(state)
        planned = self.service.agent_message("按照真实面试开始完整模拟", app_id)
        self.assertEqual(planned["proposal"]["kind"], "start_mock_interview")
        launched = self.service.confirm_agent_proposal(planned["proposal"]["proposal_id"], app_id)
        self.assertEqual(launched["thread"]["mode"], "mock_interview")
        self.assertEqual(launched["result"]["session"]["phase"], "awaiting_answer")


if __name__ == "__main__":
    unittest.main()
