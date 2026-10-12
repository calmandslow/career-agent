"""Optional UI regression with scripted model responses; no real API calls."""
import sys
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright
from career_agent.live_interview import FollowDecision, OfflineInterviewExecutor
from career_agent.web_service import WebService
from web_app import create_app


class Scripted(OfflineInterviewExecutor):
    def __init__(self): self.calls = 0
    def execute(self, skill, context, model):
        if model is FollowDecision:
            self.calls += 1
            if self.calls <= 2:
                return FollowDecision(action='follow_up', reason='unclear',
                    answer_quote='并不存在的原文', question='为什么？')
            return FollowDecision(action='follow_up', reason='sufficient',
                answer_quality='complete', follow_up_kind='extension',
                rationale='在重试机制之后，进一步考察重复执行的风险。',
                answer_quote='自动重试', question='如果请求已成功但响应丢失，重试如何避免重复操作？')
        return super().execute(skill, context, model)


def run(executable):
    with tempfile.TemporaryDirectory() as temp:
        executor = Scripted()
        svc = WebService(temp, 'qwen', executor_factory=lambda provider, progress: executor)
        aid = svc.create_app('浏览器追问测试', 'AI产品经理')['application_id']
        s = svc.repo.load(aid)
        s.interview_prep_profile = {'questions':[
            {'question':'如何处理工具失败？','priority':'P0'},
            {'question':'如何验证效果？','priority':'P1'}]}
        svc.repo.save(s)
        server = uvicorn.Server(uvicorn.Config(create_app(service=svc), host='127.0.0.1', port=8064, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True); thread.start()
        while not server.started: time.sleep(.05)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=executable, args=['--no-sandbox','--disable-dev-shm-usage'])
                page = browser.new_page(); errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto('http://127.0.0.1:8064/'); page.wait_for_selector('#new-form')
                page.locator(f'[data-job="{aid}"]').click()
                page.wait_for_function("document.querySelector('#role-heading').textContent==='AI产品经理'")
                page.locator('[data-tab="mock"]').first.click()
                page.locator('#question-count').fill('2'); page.locator('#new-session').click()
                page.wait_for_function("!S.locked && document.querySelector('#mock-answer')")
                page.locator('#mock-answer').fill('失败时自动重试，并保存状态。')
                page.locator('#submit-answer').click()
                page.wait_for_function("document.querySelector('#progress').textContent.includes('一次修正仍未通过')", timeout=10000)
                assert executor.calls == 2
                page.locator('[data-interview="move_on"]').click()
                page.wait_for_function("!S.locked && S.session.main_index===1")
                assert executor.calls == 2
                assert page.locator('#mock-answer').count() == 1
                assert page.evaluate("S.session.turns[0].response_status") == 'answered'
                assert page.evaluate("S.session.decisions[0].source") == 'user'
                page.locator('#new-session').click()
                page.wait_for_function("!S.locked && S.session.main_index===0")
                page.locator('#mock-answer').fill('失败时自动重试，并保存状态。')
                page.locator('#submit-answer').click()
                page.wait_for_function("!busy() && S.session.current_kind==='follow_up' && document.querySelector('#submit-answer') && !document.querySelector('#submit-answer').disabled", timeout=10000)
                assert '延伸追问' in page.locator('#content').inner_text()
                assert page.evaluate("S.session.decisions[0].answer_quality") == 'complete'
                assert executor.calls == 3
                page.locator('#mock-answer').fill('用幂等键识别重复请求。')
                page.locator('#submit-answer').click()
                page.wait_for_function("!S.locked && S.session.main_index===1", timeout=10000)
                assert executor.calls == 3
                assert not errors, errors
                browser.close()
                print('Follow-up UI passed: repair cap, saved answer, manual move-on, complete-answer extension label and one follow-up maximum. Real API calls: 0.')
        finally:
            server.should_exit = True; thread.join(10); svc.tasks.close()


if __name__ == '__main__': run(sys.argv[1])
