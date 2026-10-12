"""Local single-user web service. Durable jobs wrap existing Harness; keys stay server-side."""
from __future__ import annotations
import copy
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from .models import ApplicationState, Intent, utc_now
from .repository import JsonStateRepository
from .material_loader import MaterialLoader
from .skill_loader import SkillLoader
from .executor_factory import create_executor
from .orchestrator import Orchestrator
from .telemetry import ProgressReporter, friendly_error_message
from .reporting import summarize_calls, load_pricing, write_run_reports
from .live_interview import SessionStore, InterviewEngine, start_session, OfflineInterviewExecutor
from .human_review import accept_pending_resume_review
from .answer_semantic import hard_check, review_answer, review_is_pass, review_key, has_passed_review, reviewed_candidate
from .answer_update import focused_context, merge_answer
from .schemas import PreparedAnswer, AnswerPrepProfile, JDProfile
from .context_builder import build_context
from .resume_renderer import render_resume_markdown
from .verifier import VerificationError

class ConflictError(ValueError): pass


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else copy.deepcopy(default)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def safe_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r'_[a-f0-9]{12}', value):
        raise ValueError('编号格式不正确')
    return value


class WebProgress(ProgressReporter):
    def __init__(self, manager, task_id):
        super().__init__(enabled=True)
        self.manager, self.task_id = manager, task_id
    def emit(self, message):
        self.manager.log(self.task_id, message)


class TaskManager:
    def __init__(self, service):
        self.service = service
        self.root = service.root / 'data' / 'web' / 'tasks'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.active = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='career-task')
        for path in self.root.glob('task_*.json'):
            t = read_json(path)
            if t['status'] in {'queued','running'}:
                t.update(status='interrupted', error='服务已重启；保存的进度可继续。', finished_at=utc_now())
                write_json(path,t)
    def path(self, task_id): return self.root / (safe_id(task_id,'task')+'.json')
    def get(self, task_id):
        with self.lock:
            p=self.path(task_id)
            if not p.exists(): raise FileNotFoundError('找不到任务')
            return read_json(p)
    def list(self, app_id=None):
        with self.lock:
            result=[read_json(p) for p in self.root.glob('task_*.json')]
        return sorted([t for t in result if (app_id is None or t['application_id']==app_id) and not (t['application_id'] and self.service.deleted_path(t['application_id']).exists())],key=lambda t:t['created_at'],reverse=True)
    def idle(self, key):
        if key in self.active: raise ConflictError('该岗位已有任务执行中，请等待完成或查看进度。')
    def start(self, action, app_id=None, payload=None, previous=None):
        with self.lock:
            key=app_id or 'materials'
            if app_id:self.service.repo.load(app_id)
            self.idle(key)
            task_id='task_'+uuid4().hex[:12]
            task={'task_id':task_id,'operation_id':previous.get('operation_id', previous['task_id']) if previous else task_id,
                  'application_id':app_id,'action':action,'payload':payload or {},'provider':previous['provider'] if previous else self.service.provider,
                  'status':'queued','created_at':utc_now(),'logs':[],'result':None,'error':None}
            write_json(self.path(task_id),task)
            self.active[key]=task_id
            self.pool.submit(self._work,task_id,key)
            return task
    def retry(self,task_id):
        with self.lock:
            t=self.get(task_id)
            if t['status'] not in {'failed','interrupted'}: raise ConflictError('仅失败或中断任务需要续跑。')
            if t.get('retried_as'): return self.get(t['retried_as'])
            new=self.start(t['action'],t['application_id'],t['payload'],previous=t)
            t['retried_as']=new['task_id'];write_json(self.path(task_id),t)
            return new
    def log(self,task_id,message):
        with self.lock:
            t=self.get(task_id)
            t['logs'].append({'at':utc_now(),'message':message})
            t['logs']=t['logs'][-160:]
            write_json(self.path(task_id),t)
    def _work(self,task_id,key):
        with self.lock:
            t=self.get(task_id);t.update(status='running',started_at=utc_now());write_json(self.path(task_id),t)
        try:
            result=self.service.execute(t,WebProgress(self,task_id))
            status='waiting_for_user' if result.get('status') in {'waiting_for_user','semantic_review_pending','semantic_review_required'} else 'completed'
            update={'status':status,'result':result}
        except Exception as exc:
            if isinstance(exc,(ValueError,RuntimeError,FileNotFoundError)) and not hasattr(exc,'status_code'):
                error=str(exc)[:650]
                # Never expose model response bodies, key or arbitrary provider messages.
                if 'API key' in error or 'Missing API key' in error: error='未配置模型 API Key，请在启动服务的终端设置。'
            else: error=friendly_error_message(exc)
            update={'status':'failed','error':error,'error_type':type(exc).__name__}
        with self.lock:
            t=self.get(task_id);t.update(**update,finished_at=utc_now())
            try:t['cost']=self.service.task_cost(t,update.get('result') or {})
            except Exception:t['cost']=None
            write_json(self.path(task_id),t)
            self.active.pop(key,None)
    def wait(self,task_id,timeout=5):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            t=self.get(task_id)
            if t['status'] not in {'queued','running'}: return t
            time.sleep(.01)
        raise TimeoutError('Task still running')
    def close(self): self.pool.shutdown(wait=True)


class WebService:
    PIPELINES={'analyze':'analyze_jd','resume':'tailor_resume','questions':'generate_interview_questions','answers':'prepare_answer'}
    def __init__(self,root,provider='qwen',executor_factory=None):
        if provider not in {'mock','qwen'}: raise ValueError('Provider must be mock or qwen')
        self.root=Path(root).resolve();self.provider=provider
        self.repo=JsonStateRepository(self.root/'data'/'applications')
        self.sessions=SessionStore(self.root/'data'/'mock_interviews')
        self.web=self.root/'data'/'web';self.web.mkdir(parents=True,exist_ok=True)
        self.loader=SkillLoader(self.root/'skills')
        self.executor_factory=executor_factory
        self.tasks=TaskManager(self)
    def executor(self,provider,progress):
        return self.executor_factory(provider,progress) if self.executor_factory else create_executor(provider,progress=progress)
    def meta_path(self,app_id): return self.web/'applications'/(safe_id(app_id,'job')+'.json')
    def meta(self,app_id): return read_json(self.meta_path(app_id),{'material_snapshot':None,'jd_sources':[],'resume_draft':None})
    def list_apps(self):
        result=[]
        for p in self.repo.directory.glob('job_*.json'):
            if self.deleted_path(p.stem).exists():continue
            s=ApplicationState.from_dict(read_json(p))
            result.append({'application_id':s.application_id,'company':s.company,'role':s.role,'current_stage':s.current_stage,'updated_at':s.updated_at})
        return sorted(result,key=lambda x:x['updated_at'],reverse=True)
    def deleted_path(self,app_id):
        return self.web/'deleted'/(safe_id(app_id,'job')+'.json')
    def deleted_apps(self):
        with self.tasks.lock:
            return sorted([{'application_id':d['application']['application_id'],'company':d['application'].get('company'),'role':d['application'].get('role'),'deleted_at':d['deleted_at']} for d in (read_json(p) for p in (self.web/'deleted').glob('job_*.json'))],key=lambda d:d['deleted_at'],reverse=True)
    def delete_app(self,app_id):
        with self.tasks.lock:
            self.tasks.idle(app_id)
            if self.deleted_path(app_id).exists():return {'status':'deleted','application_id':app_id}
            state=self.repo.load(app_id)
            write_json(self.deleted_path(app_id),{'application':state.to_dict(),'deleted_at':utc_now()})
            self.repo._path(app_id).unlink()
        return {'status':'deleted','application_id':app_id}
    def restore_app(self,app_id):
        with self.tasks.lock:
            self.tasks.idle(app_id)
            path=self.deleted_path(app_id)
            if not path.exists():raise FileNotFoundError('找不到已删除岗位')
            if self.repo._path(app_id).exists():raise ConflictError('已有相同编号的岗位，无法覆盖恢复。')
            state=ApplicationState.from_dict(read_json(path)['application'])
            self.repo.save(state);path.unlink()
        return self.get_app(app_id)

    def create_app(self,company=None,role=None):
        with self.tasks.lock:
            s=ApplicationState(company=company or None,role=role or None);s.record('application_created');self.repo.save(s)
            return self.get_app(s.application_id)
    def get_app(self,app_id):
        s=self.repo.load(app_id);data=s.to_dict();data['web']=self.meta(app_id)
        profile=s.pending_validation_output if s.current_stage=='waiting_for_human_review_interview-answer-coach' else s.answer_prep_profile
        data['answer_reviews']=[e.detail for e in s.execution_history if e.event=='answer_semantic_review']
        data['answer_review_passed']=[has_passed_review(s,a) for a in AnswerPrepProfile.model_validate(profile).answers] if profile else []
        data['answer_hashes']=[review_key(a,s) for a in AnswerPrepProfile.model_validate(profile).answers] if profile else []
        confirmed=data['web'].get('answer_confirmations',{})
        data['answer_confirmed']=[confirmed.get(str(i))==key for i,key in enumerate(data['answer_hashes'])]
        data['cost']=summarize_calls(s.llm_calls,load_pricing())
        return data
    def patch_app(self,app_id,company,role):
        with self.tasks.lock:
            self.tasks.idle(app_id);s=self.repo.load(app_id);s.company=company or None;s.role=role or None
            if s.jd_profile:
                s.jd_profile['company']=s.company;s.jd_profile['role_title']=s.role or s.jd_profile['role_title']
            s.record('job_identity_corrected');self.repo.save(s)
        return self.get_app(app_id)
    def save_jd_profile(self, app_id, profile):
        with self.tasks.lock:
            self.tasks.idle(app_id)
            s=self.repo.load(app_id)
            if not s.jd_profile:
                raise ValueError('请先分析岗位，再修正解析结果。')
            data=profile.model_dump()
            if not data['role_title'].strip() or not data['role_summary'].strip():
                raise ValueError('岗位名称和概述不能为空。')
            if data != s.jd_profile:
                previous=copy.deepcopy(s.jd_profile)
                s.invalidate_jd_outputs()
                s.jd_profile=data;s.company=data['company'] or None;s.role=data['role_title']
                s.current_stage='completed'
                s.record('jd_profile_corrected', previous=previous, corrected=data)
                self.repo.save(s)
                meta=self.meta(app_id)
                if meta.get('resume_draft'):meta['resume_draft']['status']='stale'
                meta['jd_profile_corrected_at']=utc_now()
                write_json(self.meta_path(app_id),meta)
        return self.get_app(app_id)

    def material_path(self,material_id): return self.web/'materials'/(safe_id(material_id,'mat')+'.json')
    def materials(self):
        with self.tasks.lock:
            return sorted([m for m in (read_json(p) for p in (self.web/'materials').glob('mat_*.json')) if not (m.get('application_id') and self.deleted_path(m['application_id']).exists())],key=lambda m:m['created_at'],reverse=True)
    def material(self,material_id):
        p=self.material_path(material_id)
        if not p.exists(): raise FileNotFoundError('找不到材料')
        m=read_json(p)
        if m.get('application_id'):self.repo.load(m['application_id'])
        return m
    def register_file(self,kind,name,temporary,app_id=None):
        if kind not in {'resume','portfolio','jd'}: raise ValueError('材料类型无效')
        suffix=Path(name).suffix.lower()
        if suffix not in {'.pdf','.docx','.pptx','.txt','.md','.png','.jpg','.jpeg','.webp'}: raise ValueError('不支持这个文件格式')
        with self.tasks.lock:
            if kind=='jd':
                if not app_id: raise ValueError('JD 必须关联岗位')
                self.repo.load(app_id)
            key=app_id if kind=='jd' else None
            self.tasks.idle(key or 'materials')
            mid='mat_'+uuid4().hex[:12]
            destination=self.web/'uploads'/(mid+suffix);destination.parent.mkdir(parents=True,exist_ok=True)
            Path(temporary).replace(destination)
            item={'material_id':mid,'kind':kind,'name':Path(name.replace('\\','/')).name,'application_id':app_id,
                  'created_at':utc_now(),'status':'parsing','active':False,'text':'','characters':0,'error':None,'provider':self.provider}
            write_json(self.material_path(mid),item)
            task=self.tasks.start('parse_material',key,{'material_id':mid,'suffix':suffix})
            return {'material':item,'task':task}
    def deactivate(self,mid):
        with self.tasks.lock:
            m=self.material(mid);self.tasks.idle(m.get('application_id') or 'materials')
            m['active']=False;write_json(self.material_path(mid),m)
            if m['kind']=='jd': self._refresh_jd(m['application_id'])
        return m
    def _refresh_jd(self,app_id):
        items=[m for m in self.materials() if m['active'] and m['kind']=='jd' and m['application_id']==app_id]
        items.sort(key=lambda m:m['created_at'])
        s=self.repo.load(app_id)
        s.update_materials(jd='\n\n'.join('[SOURCE: '+m['name']+']\n'+m['text'] for m in items));self.repo.save(s)
        meta=self.meta(app_id);meta['jd_sources']=[{'material_id':m['material_id'],'name':m['name']} for m in items]
        # A changed JD makes a user's edited resume stale too.
        if meta.get('resume_draft'): meta['resume_draft']['status']='stale'
        write_json(self.meta_path(app_id),meta)
    def bind_materials(self,app_id):
        with self.tasks.lock:
            self.tasks.idle(app_id)
            ready=[m for m in self.materials() if m['active'] and m['status']=='ready' and m['kind'] in {'resume','portfolio'}]
            resumes=[m for m in ready if m['kind']=='resume']
            if not resumes: raise ValueError('请先上传并解析基础简历。')
            resume=resumes[0];portfolios=[m for m in ready if m['kind']=='portfolio']
            s=self.repo.load(app_id);s.update_materials(base_resume=resume['text'],portfolio='\n\n'.join('[SOURCE: '+m['name']+']\n'+m['text'] for m in portfolios));self.repo.save(s)
            meta=self.meta(app_id);meta['material_snapshot']={'bound_at':utc_now(),'resume':{'material_id':resume['material_id'],'name':resume['name']},
                'portfolio':[{'material_id':m['material_id'],'name':m['name']} for m in portfolios]}
            if meta.get('resume_draft'):meta['resume_draft']['status']='stale'
            write_json(self.meta_path(app_id),meta)
        return self.get_app(app_id)
    def save_draft(self,app_id,text):
        if not text.strip():raise ValueError('修改稿不能为空')
        with self.tasks.lock:
            self.tasks.idle(app_id);self.repo.load(app_id)
            meta=self.meta(app_id);meta['resume_draft']={'text':text,'status':'user_edited_unreviewed','saved_at':utc_now()};write_json(self.meta_path(app_id),meta)
        return meta['resume_draft']
    def approve(self,app_id):
        with self.tasks.lock:
            self.tasks.idle(app_id);s=self.repo.load(app_id)
            if s.current_stage=='waiting_for_human_review_resume-tailor':
                accept_pending_resume_review(s)
                result={'status':'completed'}
            elif s.current_stage=='waiting_for_human_review_interview-answer-coach':
                from recheck_answer_coach import recheck
                result=recheck(s,approve=True)
            else: raise ValueError('当前没有待确认的生成结果')
            self.repo.save(s)
        return result
    def confirm_answer(self,app_id,index,answer_hash):
        with self.tasks.lock:
            self.tasks.idle(app_id);state=self.repo.load(app_id)
            pending=state.current_stage=='waiting_for_human_review_interview-answer-coach'
            profile=state.pending_validation_output if pending else state.answer_prep_profile
            if not profile or not 0<=index<len(profile['answers']):raise ValueError('找不到这道准备答案')
            answers=AnswerPrepProfile.model_validate(profile).answers
            answer=answers[index];key=review_key(answer,state)
            if key!=answer_hash:raise ConflictError('这道答案已经更新，请刷新后重新核对。')
            try:hard_check(answer,{**build_context('interview-answer-coach',state),'answer_hard_only':True})
            except VerificationError:return {'status':'needs_changes','question_number':index+1}
            if not has_passed_review(state,answer):return {'status':'review_required','question_number':index+1}
            meta=self.meta(app_id);confirmed=meta.setdefault('answer_confirmations',{})
            confirmed[str(index)]=key
            write_json(self.meta_path(app_id),meta)
            all_confirmed=all(confirmed.get(str(i))==review_key(a,state) for i,a in enumerate(answers))
            if pending and all_confirmed:
                result=self.approve(app_id)
                if result['status']!='completed':
                    confirmed.pop(str(index),None);write_json(self.meta_path(app_id),meta)
                return {**result,'question_number':index+1,'all_confirmed':True}
            return {'status':'confirmed','question_number':index+1,'all_confirmed':all_confirmed}

    def new_session(self,app_id,count):
        with self.tasks.lock:
            self.tasks.idle(app_id)
            s=start_session(self.repo.load(app_id),count=count,provider=self.provider);self.sessions.save(s);return s
    def session(self,app_id,sid):
        self.repo.load(app_id);s=self.sessions.load(sid)
        if s['application_id']!=app_id:raise ValueError('会话不属于当前岗位')
        return s
    def session_list(self,app_id):
        self.repo.load(app_id)
        result=[read_json(p) for p in self.sessions.root.glob('mock_*.json')]
        return sorted([{'session_id':s['session_id'],'created_at':s['created_at'],'provider':s['provider'],'phase':s['phase'],'turn_count':len(s['turns'])} for s in result if s['application_id']==app_id],key=lambda s:s['created_at'],reverse=True)
    def start_task(self,action,app_id,payload=None):
        self.repo.load(app_id);payload=payload or {}
        if action not in {*self.PIPELINES,'review_answers','update_answer','interview'}:raise ValueError('任务操作无效')
        with self.tasks.lock:
            self.tasks.idle(app_id)
            s=self.repo.load(app_id)
            if action in self.PIPELINES and s.pending_validation_output:raise ConflictError('请先处理当前待人工确认结果，避免覆盖。')
            if action in {'resume','questions','answers'} and not s.base_resume:raise ValueError('请先为这个岗位关联基础材料。')
            if action in self.PIPELINES and not s.jd:raise ValueError('请先上传并解析岗位 JD。')
            if action=='interview':
                session=self.session(app_id,payload['session_id'])
                if payload['operation'] not in {'answer','skip','finish','resume','feedback','move_on'}:raise ValueError('面试操作无效')
                payload={**payload,'expected_turn_count':len(session['turns']), 'expected_feedback_completed_at':session.get('completed_at')}
            if action=='update_answer':focused_context(s,int(payload['question_index']),payload.get('feedback',''))
            return self.tasks.start(action,app_id,payload)
    def execute(self,task,progress):
        action=task['action'];app_id=task['application_id'];p=task['payload'];provider=task['provider']
        progress.emit('开始执行：'+action)
        if action=='parse_material':return self._parse(task,progress)
        if action=='interview':
            s=self.session(app_id,p['session_id'])
            factory=lambda: OfflineInterviewExecutor() if s['provider']=='mock' else self.executor(s['provider'],progress)
            engine=InterviewEngine(self.sessions,s,factory)
            operation=p['operation']
            if operation in {'answer','skip'} and s['phase']=='awaiting_answer' and len(s['turns'])==p['expected_turn_count']:
                engine.submit(p.get('answer','')) if operation=='answer' else engine.skip()
            elif operation=='finish' and s['phase']!='completed':engine.finish_early()
            elif operation=='move_on':
                if s['phase']=='evaluate' and len(s['turns'])==p['expected_turn_count']:
                    engine.move_on()
                elif len(s['turns'])!=p['expected_turn_count'] or s['phase'] not in {'advance','summary'}:
                    # A retried old request must not skip/evaluate a newer turn.
                    return {'status':'completed','session_id':s['session_id'],'phase':s['phase']}
            elif operation=='feedback':
                if not (s['phase']=='completed' and s.get('completed_at')!=p.get('expected_feedback_completed_at')):
                    engine.redo_feedback()
            engine.advance()
            return {'status':'completed','session_id':s['session_id'],'phase':s['phase']}
        if action in self.PIPELINES:
            state=self.repo.load(app_id)
            if state.pending_validation_output:raise ConflictError('当前有待确认结果，请先审查。')
            executor=self.executor(provider,progress)
            orch=Orchestrator(self.repo,self.loader,executor,progress)
            try:
                result=orch.run(app_id,Intent(self.PIPELINES[action])).to_dict()
                state=self.repo.load(app_id)
                if state.jd_profile:
                    # Preserve user's identity overrides when present.
                    state.company=state.company or state.jd_profile.get('company')
                    state.role=state.role or state.jd_profile.get('role_title')
                    self.repo.save(state)
                return result
            finally:
                state=self.repo.load(app_id)
                write_run_reports(self.root/'outputs'/app_id/'web_runs'/task['task_id'],state,
                    {'status':state.current_stage,'run_id':orch.last_run_id},run_id=orch.last_run_id)
        executor=self.executor(provider,progress)
        set_id=getattr(executor,'set_run_id',None)
        if set_id:set_id(task['task_id'])
        state=self.repo.load(app_id)
        try:
            if action=='review_answers':
                profile=AnswerPrepProfile.model_validate(state.pending_validation_output if state.current_stage=='waiting_for_human_review_interview-answer-coach' else state.answer_prep_profile)
                context=build_context('interview-answer-coach',state);reviews=[]
                for i,answer in enumerate(profile.answers):
                    if has_passed_review(state,answer):continue
                    hard_check(answer,context)
                    if provider=='mock':
                        from .answer_semantic import AnswerReview
                        review=AnswerReview(verdict='pass',issues=[])
                    else: review=review_answer(executor,answer,context)
                    item={'question_number':i+1,'answer_hash':review_key(answer,state),**review.model_dump()};reviews.append(item)
                    state.record('answer_semantic_review',**item)
                    if review_is_pass(review):state.record('answer_semantic_passed',answer_index=i,answer_hash=review_key(answer,state))
                    self.repo.save(state);progress.emit(f'第 {i+1} 题审查：{review.verdict}')
                return {'status':'completed','reviews':reviews}
            if action=='update_answer':
                index=int(p['question_index']);context=focused_context(state,index,p['feedback'])
                directory=self.root/'outputs'/app_id/'answer_updates'/task['operation_id'];directory.mkdir(parents=True,exist_ok=True)
                candidate=directory/'candidate_answer.json'
                if candidate.exists():answer=PreparedAnswer.model_validate(read_json(candidate));progress.emit('复用已保存的单题候选答案')
                elif provider=='mock':
                    profile=state.pending_validation_output if state.current_stage=='waiting_for_human_review_interview-answer-coach' else state.answer_prep_profile
                    answer=PreparedAnswer.model_validate(profile['answers'][index]);write_json(candidate,answer.model_dump())
                else:
                    answer=executor.execute(self.loader.load('interview-answer-coach'),context,PreparedAnswer);write_json(candidate,answer.model_dump())
                if provider=='mock':
                    hard_check(answer,context)
                else:
                    answer,review=reviewed_candidate(executor,self.loader.load('interview-answer-coach'),answer,context,directory)
                    if not review_is_pass(review):return {'status':'semantic_review_pending','reviews':review.model_dump()}
                result=merge_answer(state,index,answer,hard_only=True)
                state.record('answer_semantic_passed',answer_hash=review_key(answer,state),answer_index=index)
                return result
            raise ValueError('任务操作无效')
        finally:
            drain=getattr(executor,'drain_call_records',None)
            if drain:state.llm_calls.extend(drain())
            self.repo.save(state)
            write_run_reports(self.root/'outputs'/app_id/'web_runs'/task['task_id'],state,{'status':state.current_stage},run_id=task['task_id'])
    def _parse(self,task,progress):
        p=task['payload'];m=self.material(p['material_id']);executor=None
        try:
            if m['status']=='ready' and m.get('text'):
                if m['kind']=='jd':
                    with self.tasks.lock:self._refresh_jd(m['application_id'])
                progress.emit('复用已解析材料，不重复识别。')
                return {'status':'completed','material_id':m['material_id']}
            path=self.web/'uploads'/(m['material_id']+p['suffix'])
            if p['suffix'] in MaterialLoader.IMAGE_SUFFIXES:
                if task['provider']=='mock':raise ValueError('离线模式不识别图片，请用 TXT/Markdown/PDF 文本验证，或切换真实模型模式。')
                executor=self.executor(task['provider'],progress)
                setter=getattr(executor,'set_run_id',None)
                if setter:setter(task['task_id'])
            loader=MaterialLoader(image_reader=executor.extract_image_text if executor else None)
            loaded=loader.load(path)
            m.update(text=loaded.text,characters=len(loaded.text),status='ready',active=True,error=None)
            with self.tasks.lock:
                if m['kind']=='resume':
                    for old in self.materials():
                        if old['kind']=='resume' and old['active']:
                            old['active']=False;write_json(self.material_path(old['material_id']),old)
                write_json(self.material_path(m['material_id']),m)
                if m['kind']=='jd':self._refresh_jd(m['application_id'])
            progress.emit('解析完成：'+m['name'])
            return {'status':'completed','material_id':m['material_id']}
        except Exception:
            m.update(status='failed',active=False,error='解析失败。扫描 PDF 请改用图片；也可检查文件或稍后续跑。');write_json(self.material_path(m['material_id']),m)
            raise
        finally:
            if executor:
                calls=executor.drain_call_records();m=self.material(m['material_id']);m.setdefault('llm_calls',[]).extend(calls);write_json(self.material_path(m['material_id']),m)
    def task_cost(self,task,result):
        if task['action']=='parse_material':
            calls=self.material(task['payload']['material_id']).get('llm_calls',[])
            calls=[c for c in calls if c.get('run_id')==task['task_id']]
        elif task['action']=='interview':
            calls=self.sessions.load(task['payload']['session_id']).get('llm_calls',[])
            calls=[c for c in calls if c.get('started_at','')>=task.get('started_at',task['created_at'])]
        else:
            calls=self.repo.load(task['application_id']).llm_calls
            calls=[c for c in calls if c.get('run_id') in {task['task_id'],result.get('run_id')}]
        return summarize_calls(calls,load_pricing())
    def download(self,app_id,kind,sid=None):
        state=self.repo.load(app_id)
        if kind=='resume':return state.tailored_resume or '', 'tailored_resume.md'
        if kind=='draft':return (self.meta(app_id).get('resume_draft') or {}).get('text',''), 'user_edited_resume.md'
        if kind=='answers':
            from .resume_renderer import render_answer_prep_markdown
            profile=state.pending_validation_output if state.current_stage=='waiting_for_human_review_interview-answer-coach' else state.answer_prep_profile
            if not profile:raise ValueError('尚无面试答案')
            prefix='# 待人工确认\n\n' if state.current_stage=='waiting_for_human_review_interview-answer-coach' else ''
            return prefix+render_answer_prep_markdown(profile),'interview_answers.md'
        if kind in {'feedback','transcript'}:
            session=self.session(app_id,sid);lines=[]
            if kind=='transcript':
                lines=['# 模拟面试记录','']
                for t in session['turns']:lines.extend([f"## {t['turn_id']}. {t['question']}", '',t['answer'],''])
            else:
                feedback=session.get('feedback')
                if not feedback:raise ValueError('反馈尚未完成')
                lines=['# 教练反馈','',feedback['overall_assessment'],'']
                if session.get('feedback_revision_pending'):lines.extend(['新反馈尚未完成；以下为旧版反馈。',''])
                for q in feedback.get('questions',[]):
                    level={'complete':'回答完整','needs_more':'需要补充','unable':'暂不会回答','not_evaluated':'未评价'}[q['level']]
                    lines.extend([f"## 第 {q['main_index']+1} 题：{level}",session['questions'][q['main_index']],''])
                    for key,title in [('what_worked','值得保留'),('gaps','必要补充'),('optional_tips','可选建议'),('fact_notes','事实确认'),('reasoning_steps','思考步骤')]:
                        if q[key]:lines.extend(['### '+title,'',*[('- '+v) for v in q[key]],''])
                    for key,title in [('supplement','局部补充'),('reference_answer','参考回答（请核实事实）'),('practice_follow_up','建议练习')]:
                        if q[key]:lines.extend(['### '+title,'',q[key],''])
            return '\n'.join(lines),kind+'.md'
        if kind=='session':return json.dumps(self.session(app_id,sid),ensure_ascii=False,indent=2),'interview_session.json'
        if kind=='state':return json.dumps(self.get_app(app_id),ensure_ascii=False,indent=2),'application.json'
        raise ValueError('下载类型无效')
