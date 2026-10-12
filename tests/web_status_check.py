"""Optional real browser regression: per-page task state, navigation, retries and logs."""
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4
import uvicorn
from playwright.sync_api import sync_playwright
from career_agent.models import utc_now
from career_agent.web_service import WebService, write_json
from career_agent.skill_loader import SkillLoader
from web_app import create_app
ROOT=Path(__file__).resolve().parents[1]
def run(executable):
    with tempfile.TemporaryDirectory() as temp:
        svc=WebService(temp,'mock');svc.loader=SkillLoader(ROOT/'skills')
        aid=svc.create_app('隔离测试公司','AI产品经理')['application_id']
        second=svc.create_app('第二公司','另一岗位')['application_id']
        s=svc.repo.load(aid);s.update_materials(jd='AI产品经理，负责Agent产品设计、需求分析。',base_resume='应用统计硕士，个人项目设计Agent工作流。');svc.repo.save(s)
        t=svc.start_task('questions',aid);assert svc.tasks.wait(t['task_id'])['status']=='completed'
        session=svc.new_session(aid,1);sid=session['session_id']
        s=svc.repo.load(aid);s.current_stage='running_mock-interview';svc.repo.save(s)
        def fixture(action,app_id,status='failed',payload=None,error='面试权限失败测试'):
            tid='task_'+uuid4().hex[:12]
            task={'task_id':tid,'operation_id':tid,'application_id':app_id,'action':action,'payload':payload or {},'provider':'qwen','status':status,'created_at':utc_now(),'logs':[{'message':'[LLM] mock-interview started · model=example · attempt 1/3'}],'error':error if status=='failed' else None,'result':None,'cost':{'total_tokens':1234,'completed_calls':1,'retries':1,'estimated_cost_cny':0.02}}
            write_json(svc.tasks.path(tid),task);return task
        failed=fixture('interview',aid,payload={'session_id':sid,'operation':'feedback'})
        global_failed=fixture('parse_material',None,error='基础材料解析失败测试')
        server=uvicorn.Server(uvicorn.Config(create_app(service=svc),host='127.0.0.1',port=8062,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        while not server.started:time.sleep(.05)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=executable,args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:8062/');page.wait_for_selector('#new-form')
                assert page.locator('.nav-jobs [data-page="tasks"]').count()==0
                assert page.locator('.nav-tools [data-page="tasks"]').count()==1
                page.locator(f'[data-job="{aid}"]').click();page.wait_for_function("document.querySelector('#role-heading').textContent==='AI产品经理'")
                for tab in ['overview','resume','prep']:
                    page.locator(f'[data-tab="{tab}"]').first.click()
                    assert '面试权限失败测试' not in page.locator('#progress').inner_text()
                    assert '基础材料解析失败测试' not in page.locator('#progress').inner_text()
                    assert page.locator('#stage').inner_text()!='执行中'
                page.locator('[data-tab="mock"]').first.click()
                page.locator(f'[data-session="{sid}"]').click()
                page.get_by_text('面试权限失败测试',exact=True).wait_for()
                assert page.locator('#progress details[open]').count()==0
                assert '1234' not in page.locator('#progress').inner_text()
                page.locator('#progress summary').click()
                page.wait_for_timeout(2200)
                assert page.locator('#progress details[open]').count()==1
                assert '1234' in page.locator('#progress').inner_text()
                page.evaluate("notify('正在从保存的状态继续。')")
                page.locator('[data-page="new"]').click()
                assert page.locator('#notice').is_hidden();assert page.locator('#progress').inner_text()==''
                page.locator(f'[data-job="{second}"]').click();page.wait_for_function("document.querySelector('#role-heading').textContent==='另一岗位'")
                assert '面试权限失败测试' not in page.locator('#progress').inner_text()
                page.locator('[data-page="materials"]').click()
                page.get_by_text('基础材料解析失败测试',exact=True).wait_for()
                assert '面试权限失败测试' not in page.locator('#progress').inner_text()
                page.locator('[data-page="tasks"]').click()
                assert page.locator('#progress').inner_text()==''
                assert page.locator('#content details[open]').count()==0
                assert '面试权限失败测试' in page.locator('#content').inner_text()
                assert '隔离测试公司' in page.locator('#content').inner_text()
                page.locator(f'[data-job="{aid}"]').click();page.wait_for_function("document.querySelector('#role-heading').textContent==='AI产品经理'")
                page.locator('[data-tab="mock"]').first.click()
                running=fixture('interview',aid,'running',{'session_id':sid,'operation':'feedback'})
                failed['retried_as']=running['task_id'];write_json(svc.tasks.path(failed['task_id']),failed)
                page.wait_for_function("document.querySelector('#progress').textContent.includes('正在生成教练反馈')",timeout=10000)
                assert '面试权限失败测试' not in page.locator('#progress').inner_text()
                running['payload']['operation']='answer';write_json(svc.tasks.path(running['task_id']),running)
                current=svc.sessions.load(sid);current['phase']='summary';svc.sessions.save(current)
                page.wait_for_timeout(2200)
                assert '正在生成教练反馈' in page.locator('#progress').inner_text()
                # New session has no inherited failed/running status from an older session.
                new=svc.new_session(aid,1)
                page.evaluate('refresh()')
                page.wait_for_selector(f'[data-session="{new["session_id"]}"]',state='attached')
                page.locator('#content > details > summary').click()
                page.locator(f'[data-session="{new["session_id"]}"]').click()
                page.wait_for_function("document.querySelector('#progress').textContent.includes('等待你的回答')")
                assert '正在生成教练反馈' not in page.locator('#progress').inner_text()
                assert not errors,errors
                browser.close()
                print('Status regression passed: sidebar sections, per-page/job/session isolation, collapsed cost/log details, cleared notices, retry supersession and live polling. JS errors: 0.')
        finally:server.should_exit=True;thread.join(10);svc.tasks.close()
if __name__=='__main__':run(sys.argv[1])
