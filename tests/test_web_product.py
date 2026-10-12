import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from career_agent.web_service import WebService, write_json, read_json, ConflictError
from career_agent.models import ApplicationState
from web_app import create_app

ROOT=Path(__file__).resolve().parents[1]
class WebProductTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.svc=WebService(self.root,'mock');self.svc.loader=__import__('career_agent.skill_loader',fromlist=['SkillLoader']).SkillLoader(ROOT/'skills')
        self.client=TestClient(create_app(service=self.svc))
    def tearDown(self):
        self.svc.tasks.close();self.client.close();self.temp.cleanup()
    def post(self,path,body=None):
        r=self.client.post('/api'+path,json=body) if body is not None else self.client.post('/api'+path)
        self.assertLess(r.status_code,300,r.text);return r.json()
    def new(self):return self.post('/applications',{'company':'目标公司','role':'AI产品经理'})['application_id']
    def upload(self,kind,text,name='test.txt',app_id=None):
        data={'kind':kind}
        if app_id:data['application_id']=app_id
        r=self.client.post('/api/materials',data=data,files={'file':(name,text.encode(), 'text/plain')});self.assertEqual(r.status_code,200,r.text)
        task=self.svc.tasks.wait(r.json()['task']['task_id']);self.assertEqual(task['status'],'completed',task)
        return r.json()['material']['material_id']
    def job(self,aid,action):
        task=self.post('/applications/'+aid+'/tasks',{'action':action});return self.svc.tasks.wait(task['task_id'])
    def prepared(self):
        aid=self.new();self.upload('resume','姓名：测试用户\n个人项目：设计了Agent工作流和评估指标。\n教育：应用统计硕士。')
        self.upload('portfolio','PickBetter：个人产品规划实践，完成原型和指标设计。')
        self.upload('jd','AI产品经理，负责Agent流程与需求分析。要求数据分析和沟通能力。',app_id=aid)
        self.post('/applications/'+aid+'/bind-materials')
        return aid
    def test_full_http_workflow_and_persistence(self):
        aid=self.prepared()
        for action in ['analyze','resume','questions','answers']:
            task=self.job(aid,action);self.assertEqual(task['status'],'completed',task)
        s=self.client.get('/api/applications/'+aid).json()
        self.assertTrue(s['jd_profile']);self.assertTrue(s['tailored_resume']);self.assertTrue(s['answer_prep_profile'])
        task=self.job(aid,'review_answers');self.assertEqual(task['status'],'completed',task)
        self.assertTrue(all(self.client.get('/api/applications/'+aid).json()['answer_review_passed']))
        before=self.svc.repo.load(aid).to_dict()
        session=self.post('/applications/'+aid+'/sessions',{'count':1});sid=session['session_id']
        first=self.post(f'/applications/{aid}/sessions/{sid}/actions',{'operation':'answer','answer':'先澄清目标和验收标准，再比较方案。'})
        self.assertEqual(self.svc.tasks.wait(first['task_id'])['status'],'completed')
        self.assertEqual(self.client.get(f'/api/applications/{aid}/sessions/{sid}').json()['current_kind'],'follow_up')
        second=self.post(f'/applications/{aid}/sessions/{sid}/actions',{'operation':'answer','answer':'我会观察核心指标和护栏指标。'})
        self.assertEqual(self.svc.tasks.wait(second['task_id'])['status'],'completed')
        final=self.client.get(f'/api/applications/{aid}/sessions/{sid}').json()
        self.assertEqual(final['phase'],'completed');self.assertEqual(len(final['turns']),2);self.assertTrue(final['feedback']['questions'])
        self.assertEqual(self.svc.repo.load(aid).to_dict(),before)
        self.assertEqual(self.client.get(f'/api/applications/{aid}/download/session?session_id={sid}').status_code,200)
    def test_new_job_does_not_replace_old_and_snapshot_does_not_change(self):
        aid=self.prepared();old=self.svc.repo.load(aid).base_resume
        self.upload('resume','新的基础简历，与旧版不同。',name='new.txt')
        aid2=self.new();self.post('/applications/'+aid2+'/bind-materials')
        self.assertEqual(self.svc.repo.load(aid).base_resume,old)
        self.assertNotEqual(self.svc.repo.load(aid2).base_resume,old)
        self.assertEqual(len(self.client.get('/api/applications').json()),2)
    def test_material_preview_and_bad_format(self):
        mid=self.upload('portfolio','真实提取文字',name='portfolio.md')
        materials=self.client.get('/api/materials').json();self.assertEqual(materials[0]['text'],'真实提取文字')
        bad=self.client.post('/api/materials',data={'kind':'resume'},files={'file':('bad.exe',b'no')})
        self.assertEqual(bad.status_code,422)
        self.post('/materials/'+mid+'/deactivate');self.assertFalse(self.svc.material(mid)['active'])
    def test_mock_image_fails_explicitly_and_retains_file_for_retry(self):
        aid=self.new();r=self.client.post('/api/materials',data={'kind':'jd','application_id':aid},files={'file':('jd.png',b'fake')})
        task=self.svc.tasks.wait(r.json()['task']['task_id']);self.assertEqual(task['status'],'failed');self.assertIn('离线模式',task['error'])
        self.assertEqual(self.svc.material(r.json()['material']['material_id'])['status'],'failed')
    def test_no_duplicate_parallel_task_or_mutation_and_retry_deduplicates(self):
        aid=self.prepared();entered=threading.Event();release=threading.Event()
        def slow(task,progress):entered.set();release.wait(3);raise RuntimeError('模拟断网')
        with patch.object(self.svc,'execute',side_effect=slow):
            task=self.post('/applications/'+aid+'/tasks',{'action':'analyze'});self.assertTrue(entered.wait(1))
            self.assertEqual(self.client.post('/api/applications/'+aid+'/tasks',json={'action':'analyze'}).status_code,409)
            self.assertEqual(self.client.patch('/api/applications/'+aid,json={'company':'变化','role':'新岗位'}).status_code,409)
            release.set();self.assertEqual(self.svc.tasks.wait(task['task_id'])['status'],'failed')
        resumed=self.post('/tasks/'+task['task_id']+'/retry');self.svc.tasks.wait(resumed['task_id'])
        again=self.post('/tasks/'+task['task_id']+'/retry');self.assertEqual(again['task_id'],resumed['task_id'])
    def test_user_edit_is_separate_and_not_approved(self):
        aid=self.prepared();self.job(aid,'resume');original=self.svc.repo.load(aid).tailored_resume
        r=self.client.put('/api/applications/'+aid+'/resume-draft',json={'text':'用户新增、尚未审查的表述'});self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['status'],'user_edited_unreviewed');self.assertEqual(self.svc.repo.load(aid).tailored_resume,original)
    def test_pending_candidate_prevents_accidental_regeneration(self):
        aid=self.prepared();s=self.svc.repo.load(aid);s.pending_validation_output={'candidate':'saved'};self.svc.repo.save(s)
        r=self.client.post('/api/applications/'+aid+'/tasks',json={'action':'resume'});self.assertEqual(r.status_code,409)
        self.assertEqual(self.svc.repo.load(aid).pending_validation_output,{'candidate':'saved'})
    def test_cross_origin_and_host_rejected_and_config_never_returns_key(self):
        with patch.dict('os.environ',{'DASHSCOPE_API_KEY':'SECRET-TEST'}):
            r=self.client.get('/api/config');self.assertNotIn('SECRET-TEST',r.text);self.assertTrue(r.json()['api_key_configured'])
        self.assertEqual(self.client.get('/api/applications',headers={'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/config',headers={'Host':'evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/applications/job_wrong').status_code,422)
    def test_startup_marks_inflight_task_interrupted(self):
        t={'task_id':'task_123456789abc','action':'analyze','application_id':None,'provider':'mock','payload':{},'created_at':'now','logs':[],'status':'running'}
        path=self.root/'data'/'web'/'tasks'/'task_123456789abc.json';write_json(path,t)
        fresh=WebService(self.root,'mock')
        try:self.assertEqual(fresh.tasks.get(t['task_id'])['status'],'interrupted')
        finally:fresh.tasks.close()
    def test_resume_failed_interview_does_not_duplicate_saved_answer(self):
        aid=self.prepared();self.job(aid,'questions');s=self.post('/applications/'+aid+'/sessions',{'count':1})
        original_advance=__import__('career_agent.live_interview',fromlist=['InterviewEngine']).InterviewEngine.advance
        with patch('career_agent.web_service.InterviewEngine.advance',side_effect=RuntimeError('network')):
            task=self.post(f'/applications/{aid}/sessions/{s["session_id"]}/actions',{'operation':'answer','answer':'真实回答'})
            self.assertEqual(self.svc.tasks.wait(task['task_id'])['status'],'failed')
        self.assertEqual(len(self.svc.session(aid,s['session_id'])['turns']),1)
        retry=self.post('/tasks/'+task['task_id']+'/retry');self.assertEqual(self.svc.tasks.wait(retry['task_id'])['status'],'completed')
        self.assertEqual(len(self.svc.session(aid,s['session_id'])['turns']),1)
    def test_completed_skill_is_not_called_again(self):
        aid=self.prepared();self.job(aid,'analyze');state=self.svc.repo.load(aid)
        events=sum(e.event=='skill_completed' for e in state.execution_history)
        self.job(aid,'analyze');self.assertEqual(sum(e.event=='skill_completed' for e in self.svc.repo.load(aid).execution_history),events)
    def test_single_answer_update_review_and_human_approval(self):
        aid=self.prepared();self.job(aid,'answers')
        before=self.svc.repo.load(aid).answer_prep_profile['answers']
        task=self.post('/applications/'+aid+'/tasks',{'action':'update_answer','question_index':0,'feedback':'只补充这一题，不虚构事实'})
        self.assertEqual(self.svc.tasks.wait(task['task_id'])['status'],'waiting_for_user')
        state=self.svc.repo.load(aid)
        self.assertEqual(state.pending_validation_output['answers'][1:],before[1:])
        review=self.job(aid,'review_answers');self.assertEqual(review['status'],'completed')
        self.assertEqual(self.post('/applications/'+aid+'/approve')['status'],'completed')
        self.assertIsNone(self.svc.repo.load(aid).pending_validation_output)
    def test_cost_does_not_prevent_terminal_status_and_export_feedback(self):
        aid=self.prepared();self.job(aid,'questions');session=self.post('/applications/'+aid+'/sessions',{'count':1})
        task=self.post(f'/applications/{aid}/sessions/{session["session_id"]}/actions',{'operation':'skip'})
        finished=self.svc.tasks.wait(task['task_id']);self.assertEqual(finished['status'],'completed')
        self.assertEqual(finished['cost']['completed_calls'],0)
        response=self.client.get(f'/api/applications/{aid}/download/feedback?session_id={session["session_id"]}')
        self.assertEqual(response.status_code,200);self.assertIn('未评价',response.text)
    def test_completed_feedback_and_parsed_material_receipts_skip_model(self):
        from career_agent.telemetry import ProgressReporter
        aid=self.prepared();self.job(aid,'questions');session=self.post('/applications/'+aid+'/sessions',{'count':1})
        task=self.post(f'/applications/{aid}/sessions/{session["session_id"]}/actions',{'operation':'skip'});self.svc.tasks.wait(task['task_id'])
        saved=self.svc.session(aid,session['session_id'])
        replay={'action':'interview','application_id':aid,'provider':'mock','payload':{'session_id':session['session_id'],
            'operation':'feedback','expected_feedback_completed_at':'older','expected_turn_count':1}}
        with patch('career_agent.web_service.InterviewEngine._call',side_effect=AssertionError('completed work must not call')):
            self.assertEqual(self.svc.execute(replay,ProgressReporter())['phase'],'completed')
        material=[m for m in self.svc.materials() if m['kind']=='jd'][0]
        replay={'action':'parse_material','application_id':aid,'provider':'qwen','task_id':'task_123456789abc',
                'payload':{'material_id':material['material_id'],'suffix':'.png'}}
        with patch.object(self.svc,'executor',side_effect=AssertionError('ready material must not call')):
            self.assertEqual(self.svc.execute(replay,ProgressReporter())['status'],'completed')
    def test_http_page_and_static_assets(self):
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertIn('工作台',self.client.get('/').text)
        self.assertEqual(self.client.get('/static/app.js').status_code,200)

    def test_jd_correction_invalidates_outputs_preserves_sources_and_sessions(self):
        aid=self.prepared()
        for action in ['analyze','resume','questions','answers']:self.job(aid,action)
        sid=self.post('/applications/'+aid+'/sessions',{'count':1})['session_id']
        session_before=self.client.get(f'/api/applications/{aid}/sessions/{sid}').json()
        before=self.svc.repo.load(aid);profile=before.jd_profile.copy();profile['role_summary']='用户核对后的岗位概述'
        response=self.client.put('/api/applications/'+aid+'/jd-profile',json=profile)
        self.assertEqual(response.status_code,200,response.text)
        after=self.svc.repo.load(aid)
        self.assertEqual(after.jd,before.jd);self.assertEqual(after.base_resume,before.base_resume)
        self.assertEqual(after.jd_profile['role_summary'],profile['role_summary'])
        for field in ['tailored_resume','interview_prep_profile','answer_prep_profile','pending_validation_output']:
            self.assertIsNone(getattr(after,field))
        self.assertEqual(self.client.get(f'/api/applications/{aid}/sessions/{sid}').json(),session_before)
        self.assertEqual(self.job(aid,'resume')['status'],'completed')
        self.assertEqual(self.svc.repo.load(aid).jd_profile,profile)

    def test_unchanged_jd_keeps_outputs_and_empty_title_rejected(self):
        aid=self.prepared();self.job(aid,'questions');before=self.svc.repo.load(aid).to_dict()
        url='/api/applications/'+aid+'/jd-profile'
        self.assertEqual(self.client.put(url,json=before['jd_profile']).status_code,200)
        self.assertEqual(self.svc.repo.load(aid).to_dict(),before)
        profile=before['jd_profile'].copy();profile['role_title']='  '
        self.assertEqual(self.client.put(url,json=profile).status_code,422)
        self.assertEqual(self.svc.repo.load(aid).to_dict(),before)

    def test_jd_edit_requires_analysis(self):
        aid=self.prepared();self.job(aid,'analyze');profile=self.svc.repo.load(aid).jd_profile
        new=self.new();response=self.client.put('/api/applications/'+new+'/jd-profile',json=profile)
        self.assertEqual(response.status_code,422)

    def test_delete_restore_preserves_materials_sessions_and_history(self):
        aid=self.prepared();self.job(aid,'questions');session=self.post('/applications/'+aid+'/sessions',{'count':1})
        before=self.svc.repo.load(aid).to_dict();meta=self.svc.meta(aid);tasks=self.svc.tasks.list(aid);materials=self.svc.materials()
        other=self.new()
        response=self.client.delete('/api/applications/'+aid);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/api/applications/'+aid).status_code,404)
        self.assertNotIn(aid,[a['application_id'] for a in self.svc.list_apps()])
        self.assertIn(other,[a['application_id'] for a in self.svc.list_apps()])
        self.assertEqual(self.svc.tasks.list(aid),[])
        self.assertTrue(all(not m.get('application_id') for m in self.svc.materials()))
        self.assertEqual(self.client.delete('/api/applications/'+aid).status_code,200)
        self.assertEqual(self.client.get('/api/deleted-applications').json()[0]['application_id'],aid)
        self.post('/deleted-applications/'+aid+'/restore')
        self.assertEqual(self.svc.repo.load(aid).to_dict(),before)
        self.assertEqual(self.svc.meta(aid),meta);self.assertEqual(self.svc.tasks.list(aid),tasks);self.assertEqual(self.svc.materials(),materials)
        self.assertEqual(self.client.get(f'/api/applications/{aid}/sessions/{session["session_id"]}').json(),session)
        self.assertEqual(self.client.get('/api/deleted-applications').json(),[])

    def test_delete_rejects_active_task(self):
        aid=self.new();self.svc.tasks.active[aid]='task_'+('a'*12)
        self.assertEqual(self.client.delete('/api/applications/'+aid).status_code,409)
        self.assertEqual(self.client.get('/api/applications/'+aid).status_code,200)
        self.assertFalse(self.svc.deleted_path(aid).exists());self.svc.tasks.active.pop(aid)

    def test_deleted_job_survives_restart_and_restores(self):
        aid=self.new();self.client.delete('/api/applications/'+aid)
        second=WebService(self.root,'mock')
        try:
            self.assertEqual(second.list_apps(),[]);self.assertEqual(second.deleted_apps()[0]['application_id'],aid)
            second.restore_app(aid);self.assertEqual(second.list_apps()[0]['application_id'],aid)
        finally:second.tasks.close()

    def test_confirm_answers_individually_without_api_calls(self):
        aid=self.prepared();self.job(aid,'answers')
        state=self.svc.repo.load(aid);state.pending_validation_output=state.answer_prep_profile;state.answer_prep_profile=None;state.current_stage='waiting_for_human_review_interview-answer-coach';self.svc.repo.save(state)
        original_calls=len(state.llm_calls)
        app=self.svc.get_app(aid);self.assertFalse(any(app['answer_confirmed']))
        response=self.post('/applications/'+aid+'/confirm-answer',{'question_index':0,'answer_hash':app['answer_hashes'][0]})
        self.assertEqual(response['status'],'review_required')
        self.job(aid,'review_answers')
        for i in range(len(app['answer_hashes'])):
            response=self.post('/applications/'+aid+'/confirm-answer',{'question_index':i,'answer_hash':app['answer_hashes'][i]})
            current=self.svc.get_app(aid);self.assertTrue(current['answer_confirmed'][i])
            if i<len(app['answer_hashes'])-1:self.assertIsNotNone(current['pending_validation_output'])
        self.assertEqual(response['status'],'completed');self.assertIsNone(self.svc.repo.load(aid).pending_validation_output)
        self.assertEqual(len(self.svc.repo.load(aid).llm_calls),original_calls)

    def test_confirmation_rejects_stale_hash_and_invalidates_only_changed_answer(self):
        aid=self.prepared();self.job(aid,'answers');self.job(aid,'review_answers')
        app=self.svc.get_app(aid)
        for i,key in enumerate(app['answer_hashes']):self.post('/applications/'+aid+'/confirm-answer',{'question_index':i,'answer_hash':key})
        state=self.svc.repo.load(aid);state.answer_prep_profile['answers'][0]['recommended_answer']+=' 我会明确标注假设。';self.svc.repo.save(state)
        current=self.svc.get_app(aid);self.assertFalse(current['answer_confirmed'][0]);self.assertEqual(current['answer_confirmed'][1:],[True]*(len(app['answer_hashes'])-1))
        response=self.client.post('/api/applications/'+aid+'/confirm-answer',json={'question_index':0,'answer_hash':app['answer_hashes'][0]})
        self.assertEqual(response.status_code,409)
