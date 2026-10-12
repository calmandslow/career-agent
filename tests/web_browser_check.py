"""Optional browser integration check. Uses temporary data and offline models only."""
import json
import tempfile
import threading
import time
from pathlib import Path
import uvicorn
from playwright.sync_api import sync_playwright
from career_agent.web_service import WebService
from career_agent.skill_loader import SkillLoader
from web_app import create_app

ROOT=Path(__file__).resolve().parents[1]
def run(executable):
    with tempfile.TemporaryDirectory() as temp:
        service=WebService(temp,'mock');service.loader=SkillLoader(ROOT/'skills')
        server=uvicorn.Server(uvicorn.Config(create_app(service=service),host='127.0.0.1',port=8061,log_level='error'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=executable,args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page(viewport={'width':1280,'height':900});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto('http://127.0.0.1:8061/');page.wait_for_selector('#new-form')
                page.locator('[data-page="materials"]').click()
                page.locator('[data-upload="resume"]').set_input_files({'name':'resume.txt','mimeType':'text/plain','buffer':'姓名：测试用户\n应用统计硕士\n个人项目：设计了Agent工作流和指标。'.encode()})
                page.get_by_text('材料已解析并保存。',exact=True).wait_for();page.wait_for_function("!S.locked && !document.querySelector('[data-upload=portfolio]').disabled")
                page.locator('[data-upload="portfolio"]').set_input_files({'name':'portfolio.md','mimeType':'text/plain','buffer':'PickBetter个人项目，完成原型和指标设计。'.encode()})
                page.wait_for_function("document.querySelector('#content').textContent.includes('portfolio.md') && !S.locked && !document.querySelector('[data-upload=resume]').disabled")
                page.locator('[data-upload="resume"]').set_input_files({'name':'resume-v2.txt','mimeType':'text/plain','buffer':'姓名：测试用户\n应用统计硕士\n个人项目：设计了Agent工作流和指标。'.encode()})
                page.wait_for_selector('.material-history')
                assert not page.locator('.material-history').evaluate('(el)=>el.open')
                page.locator('.material-history > summary').click()
                assert page.locator('.material-history').evaluate('(el)=>el.open')
                page.locator('[data-page="new"]').click();page.locator('[name="company"]').fill('浏览器测试公司');page.locator('[name="role"]').fill('Agent产品经理')
                page.get_by_role('button',name='创建岗位',exact=True).click();page.wait_for_selector('[data-upload="jd"]')
                page.locator('[data-upload="jd"]').set_input_files({'name':'jd.txt','mimeType':'text/plain','buffer':'AI产品经理，负责Agent流程设计、需求分析和产品验证。'.encode()})
                page.get_by_text('核对 JD 提取文字',exact=True).wait_for()
                page.locator('[data-run="analyze"]').click();page.get_by_text('岗位分析结果',exact=True).wait_for(timeout=15000)
                page.locator('#jd-editor summary').click()
                page.locator('#jd-profile-form [name="role_summary"]').fill('人工核对的岗位概述')
                page.locator('[data-tab="resume"]').first.click()
                page.get_by_text('请先保存岗位修正，再继续下一步。',exact=True).wait_for()
                page.get_by_role('button',name='保存岗位修正',exact=True).click()
                page.locator('.prose').filter(has_text='人工核对的岗位概述').wait_for()
                page.locator('[data-tab="resume"]').first.click();page.locator('#bind').click();page.get_by_text('本岗位已关联当前基础材料。',exact=True).wait_for()
                page.locator('[data-run="resume"]').click();page.wait_for_selector('#resume-draft',timeout=15000)
                page.locator('#resume-draft').fill('手动修改稿，未审查');page.wait_for_function("!S.locked && !document.querySelector('#save-draft').disabled");page.locator('#save-draft').click();page.get_by_text('修改稿已保存，尚未重新审查。',exact=True).wait_for()
                page.reload();page.wait_for_selector('[data-tab="resume"]');page.locator('[data-tab="resume"]').first.click();page.wait_for_selector('#resume-draft');assert page.locator('#resume-draft').input_value()=='手动修改稿，未审查', (page.locator('#resume-draft').input_value(),service.meta(service.list_apps()[0]['application_id']))
                page.get_by_role('button',name='下一步：面试准备',exact=True).click();page.locator('[data-run="questions"]').click();page.wait_for_function("document.querySelector('#content').textContent.includes('P0')",timeout=15000)
                page.locator('[data-run="answers"]').click();page.get_by_text('准备答案',exact=True).wait_for(timeout=15000)
                assert page.locator('.answer-card').count()==page.locator('.answer-confirmation').count()
                assert page.get_by_role('button',name='进入模拟面试',exact=True).count()==1
                assert page.evaluate("Boolean(document.querySelector('#prepared-answers').compareDocumentPosition(document.querySelector('.prep-next')) & Node.DOCUMENT_POSITION_FOLLOWING)")
                job_id=page.evaluate('S.id')
                state=service.repo.load(job_id);state.pending_validation_output=state.answer_prep_profile;state.answer_prep_profile=None;state.current_stage='waiting_for_human_review_interview-answer-coach';service.repo.save(state)
                page.reload();page.wait_for_selector('[data-tab="prep"]');page.locator('[data-tab="prep"]').first.click()
                page.wait_for_selector('.answer-confirmation')
                assert page.locator('#content > .panel > .badge.warn').count()==0
                assert '硬约束' not in page.locator('.answer-confirmation').first.inner_text()
                page.get_by_role('button',name='检查答案内容',exact=True).click()
                page.wait_for_function("document.querySelector('[data-confirm-answer=\"0\"]') && !document.querySelector('[data-confirm-answer=\"0\"]').disabled")
                count=page.locator('.answer-card').count()
                if count>1:page.locator('[data-answer-feedback="1"]').fill('保留第二题尚未提交的修改意见')
                for i in range(count):
                    page.wait_for_function('!S.locked')
                    page.locator(f'[data-confirm-answer="{i}"]').click()
                    page.locator(f'[data-answer-index="{i}"] .answer-confirmation .badge').filter(has_text='你已确认').wait_for()
                if count>1:assert page.locator('[data-answer-feedback="1"]').input_value()=='保留第二题尚未提交的修改意见'
                page.get_by_role('button',name='进入模拟面试',exact=True).click();page.locator('#question-count').fill('1');page.locator('#new-session').click();page.wait_for_selector('#mock-answer')
                page.locator('#mock-answer').fill('我会先澄清目标，再比较方案并验证。');page.locator('#submit-answer').click();page.wait_for_function("document.querySelector('#content .question')?.textContent.includes('依据')",timeout=15000)
                page.reload();page.wait_for_selector('[data-tab="mock"]');page.locator('[data-tab="mock"]').first.click();page.wait_for_selector('#mock-answer')
                assert page.locator('#content .question').inner_text().find('依据')>=0
                page.locator('#mock-answer').fill('明确核心指标和护栏指标。');page.locator('#submit-answer').click();page.get_by_text('本轮教练反馈',exact=True).wait_for(timeout=15000)
                font=ROOT.parent/'browser-check/node_modules/@fontsource/noto-sans-sc/files/noto-sans-sc-chinese-simplified-400-normal.woff2'
                if font.exists():
                    import base64
                    encoded=base64.b64encode(font.read_bytes()).decode()
                    page.add_style_tag(content='@font-face{font-family:QAChinese;src:url(data:font/woff2;base64,'+encoded+')}#career-proto,#career-proto *{font-family:QAChinese, sans-serif !important}')
                    page.evaluate('document.fonts.ready')
                page.screenshot(path=str(ROOT.parent/'web-desktop.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(ROOT.parent/'web-mobile.png'),full_page=True)
                assert not errors,errors
                # Different job preserves the first one; material page has no job tabs.
                page.locator('[data-page="materials"]').click();assert not page.locator('#job-header').is_visible()
                page.locator('[data-page="new"]').click();page.locator('[name="company"]').fill('第二家公司');page.get_by_role('button',name='创建岗位',exact=True).click();page.wait_for_selector('[data-upload="jd"]')
                assert page.locator('#jobs [data-job]').count()==2
                # One click removes a selected job with no dialog; restore retains its data.
                second_id=page.locator('#jobs [data-job]').first.get_attribute('data-job')
                page.locator(f'[data-job="{second_id}"]').click()
                page.wait_for_selector(f'[data-delete-job="{second_id}"]')
                dialogs=[];page.on('dialog',lambda d:(dialogs.append(d.message),d.dismiss()))
                page.locator(f'[data-delete-job="{second_id}"]').click()
                page.wait_for_function("document.querySelectorAll('#jobs [data-job]').length===1")
                assert page.locator('#new-form').is_visible()
                assert not dialogs,dialogs
                page.locator('#deleted-jobs > summary').click()
                page.locator(f'[data-restore-job="{second_id}"]').click()
                page.wait_for_function("document.querySelectorAll('#jobs [data-job]').length===2")
                assert page.locator(f'[data-delete-job="{second_id}"]').count()==1
                print('Browser: upload, two jobs, analysis, resume, draft persistence, questions, answers, follow-up, coaching and responsive layouts passed. JS errors: 0.')
                browser.close()
        finally:
            server.should_exit=True;thread.join(10)
if __name__=='__main__':
    import sys
    run(sys.argv[1])
