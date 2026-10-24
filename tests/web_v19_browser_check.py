"""Optional V1.9 browser acceptance check using local mock data only."""
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from career_agent.skill_loader import SkillLoader
from career_agent.web_service import WebService
from web_app import create_app


ROOT = Path(__file__).resolve().parents[1]


def run(executable):
    with tempfile.TemporaryDirectory(dir=ROOT) as temp:
        service = WebService(temp, "mock")
        service.loader = SkillLoader(ROOT / "skills")
        server = uvicorn.Server(uvicorn.Config(create_app(service=service), host="127.0.0.1", port=8063, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=executable, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://127.0.0.1:8063/")
                page.wait_for_selector("#new-form")

                page.locator('[name="company"]').fill("动态流程公司")
                page.locator('[name="role"]').fill("增长产品经理")
                page.get_by_role("button", name="创建岗位", exact=True).click()
                page.wait_for_selector("#agent-form")
                assert page.get_by_text("岗位准备度", exact=True).is_visible()

                note = "我通过一面了，接下来准备二面。一面面试官觉得我的数据归因讲得不够清楚。"
                page.locator("#agent-input").fill(note)
                page.locator("#agent-input").press("Enter")
                page.get_by_text("我识别到一次招聘进展更新。请先核对下面的变更；确认前不会修改岗位数据。", exact=True).wait_for()
                assert "一面" in page.locator("#agent-thread").inner_text()
                assert "二面" in page.locator("#agent-thread").inner_text()
                page.locator("#confirm-progress").click()
                page.get_by_text("Agent 操作已执行，结果和运行记录已保存。", exact=True).wait_for()

                page.locator('[data-tab="prep"]').first.click()
                page.get_by_text("招聘进程", exact=True).wait_for()
                assert page.locator(".stage-card").count() == 2
                page.locator(".stage-card details summary").first.click()
                assert "数据归因解释不充分" in page.locator("#content").inner_text()

                page.locator('[data-page="materials"]').click()
                resume = (ROOT / "examples" / "v19" / "base_resume_v1.md").read_bytes()
                page.locator('[data-upload="resume"]').set_input_files({"name": "base_resume_v1.md", "mimeType": "text/markdown", "buffer": resume})
                page.get_by_text("材料已解析并保存。", exact=True).wait_for()
                page.locator('[data-page="evidence"]').click()
                page.get_by_role("heading", name="职业证据库", exact=True).wait_for()
                page.locator('[data-evidence-status="confirmed"]').first.click()
                page.get_by_text("证据状态已更新。", exact=True).wait_for()
                page.locator(".status-confirmed").first.wait_for()
                assert page.locator(".status-confirmed").count() >= 1

                page.wait_for_function("!S.locked")
                page.locator(".job-select").first.click()
                page.wait_for_function("S.page === 'workspace'")
                page.locator('[data-tab="overview"]').first.click()
                page.wait_for_selector('[data-upload="jd"]')
                jd = (ROOT / "examples" / "v19" / "target_jd.md").read_bytes()
                page.locator('[data-upload="jd"]').set_input_files({"name": "target_jd.md", "mimeType": "text/markdown", "buffer": jd})
                page.get_by_text("核对 JD 提取文字", exact=True).wait_for()
                page.locator('[data-run="analyze"]').click()
                page.get_by_text("岗位分析结果", exact=True).wait_for(timeout=15000)
                assert not errors, errors
                page.screenshot(path=str(ROOT / "outputs" / "v19-browser-check.png"), full_page=True)
                browser.close()
                print("V1.9 Browser: dashboard, agent confirmation, dynamic stages and evidence confirmation passed. JS errors: 0.")
        finally:
            server.should_exit = True
            thread.join(10)


if __name__ == "__main__":
    import sys
    run(sys.argv[1])
