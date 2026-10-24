import tempfile
import unittest
from pathlib import Path

from career_agent.context_builder import build_context
from career_agent.skill_loader import SkillLoader
from career_agent.web_service import WebService


ROOT = Path(__file__).resolve().parents[1]


class V19ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = WebService(self.root, "mock")
        self.service.loader = SkillLoader(ROOT / "skills")

    def tearDown(self):
        self.service.tasks.close()
        self.temp.cleanup()

    def upload(self, kind, name, text, app_id=None):
        source = self.root / ("incoming_" + name)
        source.write_text(text, encoding="utf-8")
        receipt = self.service.register_file(kind, name, source, app_id)
        task = self.service.tasks.wait(receipt["task"]["task_id"])
        self.assertEqual(task["status"], "completed", task)
        return receipt["material"]["material_id"]

    def test_evidence_versions_confirmation_and_material_impact(self):
        app_id = self.service.create_app("目标公司", "增长产品经理")["application_id"]
        first = self.upload("resume", "resume-v1.md", "增长项目\n会员转化率相对提升 8%。")
        evidence = self.service.evidence()["items"]
        self.assertTrue(evidence)
        self.assertTrue(all(item["source_version"] == 1 for item in evidence))
        confirmed = self.service.confirm_evidence(evidence[0]["evidence_id"], "confirmed")
        self.assertEqual(confirmed["confirmation_status"], "confirmed")

        self.service.bind_materials(app_id)
        bound = self.service.repo.load(app_id).material_evidence["resume"]
        self.assertEqual(bound[0]["knowledge_id"], confirmed["evidence_id"])
        self.assertEqual(bound[0]["confirmation_status"], "confirmed")

        self.service.confirm_evidence(confirmed["evidence_id"], "rejected")
        state = self.service.repo.load(app_id)
        context = build_context("resume-tailor", state)
        rejected_ids = {
            item.get("knowledge_id")
            for items in context["candidate_evidence"].values()
            if isinstance(items, list)
            for item in items
        }
        self.assertNotIn(confirmed["evidence_id"], rejected_ids)
        self.assertEqual(self.service.get_app(app_id)["dashboard"]["actions"][0]["key"], "review_impacts")
        self.service.confirm_evidence(confirmed["evidence_id"], "confirmed")

        second = self.upload(
            "resume",
            "resume-v2.md",
            "增长项目\n设置实验组和对照组，会员转化率相对提升 8%。",
        )
        self.assertNotEqual(first, second)
        materials = [item for item in self.service.materials() if item["kind"] == "resume"]
        self.assertEqual(sorted(item["version"] for item in materials), [1, 2])
        self.assertTrue(self.service.meta(app_id)["material_impacts"][-1]["status"] == "pending")
        self.assertTrue(self.service.get_app(app_id)["dashboard"]["actions"][0]["key"] == "review_impacts")

        self.service.bind_materials(app_id)
        self.assertTrue(all(item["status"] == "resolved" for item in self.service.meta(app_id)["material_impacts"]))

    def test_progress_note_creates_dynamic_rounds_and_risk_context(self):
        app_id = self.service.create_app("目标公司", "产品经理")["application_id"]
        text = "我通过一面了，接下来准备二面。一面面试官觉得我的数据归因讲得不够清楚。"
        proposal = self.service.preview_progress(app_id, text)
        self.assertEqual(proposal["passed_stage_name"], "一面")
        self.assertEqual(proposal["next_stage_name"], "二面")
        self.assertIn("数据归因解释不充分", proposal["risks"])

        result = self.service.confirm_progress(app_id, proposal)
        process = result["web"]["recruitment_process"]
        self.assertEqual([stage["name"] for stage in process["stages"]], ["一面", "二面"])
        self.assertEqual(process["current_stage_id"], process["stages"][1]["stage_id"])
        self.assertEqual(process["stages"][0]["status"], "passed")
        self.assertIn("数据归因解释不充分", result["dashboard"]["risks"])
        context = build_context("interview-question-generator", self.service.repo.load(app_id))
        self.assertEqual(context["recruitment_process"]["current_stage_id"], process["current_stage_id"])

    def test_company_process_is_not_hardcoded(self):
        app_id = self.service.create_app("特殊流程公司", "产品经理")["application_id"]
        for name, kind, status in [
            ("线上作业", "assignment", "passed"),
            ("Case 展示", "case", "scheduled"),
            ("跨团队委员会", "panel", "planned"),
        ]:
            self.service.save_stage(app_id, {
                "name": name, "stage_type": kind, "status": status,
                "focus_areas": [], "feedback": [], "risks": [],
                "make_current": name == "Case 展示",
            })
        process = self.service.get_app(app_id)["web"]["recruitment_process"]
        self.assertEqual([stage["name"] for stage in process["stages"]], ["线上作业", "Case 展示", "跨团队委员会"])
        current = next(stage for stage in process["stages"] if stage["stage_id"] == process["current_stage_id"])
        self.assertEqual(current["stage_type"], "case")


if __name__ == "__main__":
    unittest.main()
