"""Optional V2.0 browser acceptance check using local mock data only."""
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
        server = uvicorn.Server(uvicorn.Config(create_app(service=service), host="127.0.0.1", port=8064, log_level="error"))
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=executable, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page(viewport={"width": 1500, "height": 1000})
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto("http://127.0.0.1:8064/")
                page.wait_for_selector("#new-form")
                assert "V2.0" in page.locator(".brand").inner_text()

                page.locator('[name="company"]').fill("目标公司")
                page.locator('[name="role"]').fill("增长产品经理")
                page.get_by_role("button", name="创建岗位", exact=True).click()
                page.wait_for_selector("#agent-form")
                page.get_by_text("当前岗位记忆", exact=True).wait_for()
                assert "目标公司 · 增长产品经理" in page.locator("#agent-context").inner_text()

                page.locator("#agent-input").fill("什么是机会成本？")
                page.locator("#agent-input").press("Enter")
                page.get_by_text("这是普通对话，我可以回答；这段内容不会写入职业档案。", exact=True).wait_for()
                assert page.locator("#agent-mode").inner_text() == "普通对话"

                page.locator("#agent-input").fill("第一行")
                page.locator("#agent-input").press("Shift+Enter")
                page.locator("#agent-input").type("第二行")
                assert page.locator("#agent-input").input_value() == "第一行\n第二行"
                page.locator("#agent-input").fill("")

                note = "我通过了一面，下周三参加业务二面。一面主要问增长项目，面试官觉得我的数据归因和个人贡献讲得不够清楚，帮我准备。"
                page.locator("#agent-input").fill(note)
                page.locator("#agent-input").press("Enter")
                page.get_by_text("等待你确认", exact=True).wait_for()
                agent_text = page.locator("#agent-thread").inner_text()
                assert "一面" in agent_text and "二面" in agent_text
                page.locator("#confirm-progress").click()
                page.get_by_text("Agent 操作已执行，结果和运行记录已保存。", exact=True).wait_for()

                page.locator("#agent-input").fill("开始数据归因专项训练")
                page.locator("#agent-input").press("Enter")
                page.get_by_text("专项训练", exact=True).first.wait_for()
                assert page.locator("#agent-mode").inner_text() == "专项训练"
                page.locator("#agent-input").fill("我负责实验设计，因为两个组都参加活动，所以用用户 ID 随机分流，对照比较了 12 万用户两周数据。")
                page.locator("#agent-input").press("Enter")
                page.get_by_text("继续追问", exact=False).last.wait_for()
                page.locator("#agent-input").fill("结束训练")
                page.locator("#agent-input").press("Enter")
                page.get_by_text("等待你确认", exact=True).wait_for()
                page.locator("#confirm-progress").click()
                page.wait_for_function("!S.locked && !S.agentThread.pending_proposal")

                page.locator('[data-tab="overview"]').first.click()
                page.get_by_text("最近专项训练", exact=False).wait_for()
                assert not errors, errors
                page.screenshot(path=str(ROOT / "outputs" / "v20-browser-check.png"), full_page=True)
                browser.close()
                print("V2.0 Browser: general chat isolation, confirmed progress and focused training passed. JS errors: 0.")
        finally:
            server.should_exit = True
            server_thread.join(10)


if __name__ == "__main__":
    import sys
    run(sys.argv[1])
