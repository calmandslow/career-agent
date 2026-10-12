"""Optional browser regression for document and nested-log scroll during polling.

Run: PYTHONPATH=. python tests/web_scroll_check.py /path/to/chromium
Uses temporary local data and no model API calls.
"""
import sys
import tempfile
import threading
import time
from uuid import uuid4

import uvicorn
from playwright.sync_api import sync_playwright

from career_agent.models import utc_now
from career_agent.web_service import WebService, write_json
from web_app import create_app


def run(executable):
    with tempfile.TemporaryDirectory() as temp:
        svc = WebService(temp, 'mock')
        aid = svc.create_app('滚动测试公司', 'AI产品经理')['application_id']

        def fixture(index, status='failed'):
            tid = 'task_' + uuid4().hex[:12]
            task = dict(task_id=tid, operation_id=tid, application_id=aid,
                        action='resume', payload={}, provider='qwen', status=status,
                        created_at=utc_now(), result=None, error=None,
                        logs=[{'message': f'日志 {index} 行 {j}'} for j in range(120)], cost=None)
            write_json(svc.tasks.path(tid), task)
            return task

        tasks = [fixture(i) for i in range(24)]
        server = uvicorn.Server(uvicorn.Config(create_app(service=svc),
                                host='127.0.0.1', port=8063, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(.05)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=executable,
                           args=['--no-sandbox', '--disable-dev-shm-usage'])
                page = browser.new_page(viewport={'width': 1200, 'height': 700})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto('http://127.0.0.1:8063/')
                page.wait_for_selector('#new-form')
                page.locator('[data-page="tasks"]').click()
                page.wait_for_selector('#task-list .task')
                tid = page.locator('#task-list .task').nth(12).get_attribute('data-task-id')
                page.evaluate('''tid => {
                    window.readId = tid;
                    const card = document.querySelector(`[data-task-id="${tid}"]`);
                    card.querySelector('details').open = true;
                    card.querySelector('summary').focus({preventScroll:true});
                    card.scrollIntoView();
                    card.querySelector('.log').scrollTop = 430;
                    window.readCard = card;
                    window.readY = window.scrollY;
                    window.readTop = card.getBoundingClientRect().top;
                }''', tid)
                page.wait_for_timeout(4400)  # actual two-second polling, no forced update
                assert page.evaluate('readCard === document.querySelector(`[data-task-id="${readId}"]`)')

                def check_reading():
                    assert page.evaluate('''() => {
                        const card=document.querySelector(`[data-task-id="${readId}"]`);
                        return card.querySelector('details').open &&
                          Math.abs(card.querySelector('.log').scrollTop-430)<2 &&
                          Math.abs(card.getBoundingClientRect().top-readTop)<2 &&
                          document.activeElement===card.querySelector('summary');
                    }''')

                check_reading()
                read = next(t for t in tasks if t['task_id'] == tid)
                read['logs'].append({'message': '新增日志，保持阅读位置'})
                write_json(svc.tasks.path(tid), read)
                page.wait_for_function('document.querySelector(`[data-task-id="${readId}"] .log`).textContent.includes("新增日志")')
                check_reading()
                fixture(99)  # a new task above the one being read
                page.wait_for_function('document.querySelectorAll("#task-list .task").length===25')
                check_reading()
                # Completion invokes refresh() as well as the regular task poll.
                read['status'] = 'running'
                write_json(svc.tasks.path(tid), read)
                page.wait_for_function('document.querySelector(`[data-task-id="${readId}"] > .row`).textContent.includes("执行中")')
                read['status'] = 'completed'
                write_json(svc.tasks.path(tid), read)
                page.wait_for_function('document.querySelector(`[data-task-id="${readId}"] > .row`).textContent.includes("已完成")')
                page.wait_for_timeout(2300)
                check_reading()
                page.locator(f'[data-task-details="{tid}"] summary').click()
                page.wait_for_timeout(2300)
                assert not page.locator(f'[data-task-details="{tid}"]').evaluate('(d)=>d.open')

                # The same protection also applies to a step's progress details.
                latest = fixture(100)
                page.locator(f'[data-job="{aid}"]').click()
                page.locator('[data-tab="resume"]').first.click()
                page.wait_for_selector(f'#progress [data-task-details="{latest["task_id"]}"]')
                page.evaluate('''() => {
                    const d=document.querySelector('#progress details');d.open=true;
                    d.querySelector('.log').scrollTop=310;
                }''')
                latest['logs'].append({'message': '步骤日志新增'})
                write_json(svc.tasks.path(latest['task_id']), latest)
                page.wait_for_function('document.querySelector("#progress .log").textContent.includes("步骤日志新增")')
                assert page.locator('#progress details').evaluate('(d)=>d.open && Math.abs(d.querySelector(".log").scrollTop-310)<2')
                assert not errors, errors
                browser.close()
                print('Scroll regression passed: unchanged poll, new logs, new tasks, terminal refresh, focus, collapsed details and step logs. API calls: 0.')
        finally:
            server.should_exit = True
            thread.join(10)
            svc.tasks.close()


if __name__ == '__main__':
    run(sys.argv[1])
