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
from .evidence_builder import build_material_evidence
from .v19 import (
    default_meta, ingest_evidence, normalize_meta, parse_progress_note,
    readiness, search_evidence, validate_stage,
)
from .v20 import (
    GeneralAgentReply, IntentDecision, TOPICS, assess_training_answer, default_thread,
    fallback_intent, reconcile_semantic_intent,
    hybrid_search, message as agent_message_record, normalize_thread,
    proposal as agent_proposal, route_explicit_intent, select_topic,
    stable_manual_evidence_id,
)

class ConflictError(ValueError): pass


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else copy.deepcopy(default)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(4):
        try:
            tmp.replace(path);return
        except PermissionError:
            if attempt==3:
                path.write_bytes(tmp.read_bytes());tmp.unlink(missing_ok=True);return
            time.sleep(.01*(attempt+1))


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
            if isinstance(exc,(ValueError,RuntimeError,FileNotFoundError,PermissionError)) and not hasattr(exc,'status_code'):
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
    def meta(self,app_id): return normalize_meta(read_json(self.meta_path(app_id),default_meta()))
    def evidence_path(self): return self.web/'career_evidence.json'
    def evidence_store(self): return read_json(self.evidence_path(),{'version':1,'items':[],'changes':[]})
    def evidence(self,query='',include_inactive=False):
        store=self.evidence_store()
        return {'items':search_evidence(store,query,include_inactive=include_inactive),
                'changes':list(reversed(store.get('changes',[])))[:20]}
    def agent_thread_path(self,app_id=None):
        name=safe_id(app_id,'job') if app_id else 'global'
        return self.web/'agent_threads'/(name+'.json')
    def agent_thread(self,app_id=None):
        state=self.repo.load(app_id) if app_id else None
        thread=normalize_thread(read_json(self.agent_thread_path(app_id)),app_id)
        if state:
            meta=self.meta(app_id);dashboard=readiness(state,meta,self.session_list(app_id,validate=False))
            current=dashboard.get('current_stage') or {}
            thread['context']={
                'scope':'application','company':state.company,'role':state.role,
                'current_stage':current.get('name'),'readiness':dashboard.get('overall',0),
                'risks':dashboard.get('risks',[])[:3],
                'next_action':(dashboard.get('actions') or [{}])[0].get('title'),
            }
        else:
            thread['context']={'scope':'global','company':None,'role':None,'current_stage':None,
                               'readiness':None,'risks':[],'next_action':None}
        return thread
    def _save_agent_thread(self,thread):
        thread['updated_at']=utc_now();thread['messages']=thread.get('messages',[])[-200:]
        write_json(self.agent_thread_path(thread.get('application_id')),thread)
        return thread
    def _agent_citations(self,query):
        return [{key:item.get(key) for key in ('evidence_id','text','source','source_version','confirmation_status','retrieval_score')}
                for item in hybrid_search(self.evidence_store().get('items',[]),query)]
    def _general_agent_reply(self,text,intent,app_id,citations):
        context={'user_message':text,'intent':intent}
        if intent!='general_chat' and app_id:
            state=self.repo.load(app_id);meta=self.meta(app_id)
            context.update(job={'company':state.company,'role':state.role},
                           recruitment_process=meta['recruitment_process'],
                           evidence=citations)
        executor=self.executor(self.provider,ProgressReporter(enabled=False))
        reply=executor.execute(self.loader.load('career-agent'),context,GeneralAgentReply)
        if app_id and hasattr(executor,'drain_call_records'):
            calls=executor.drain_call_records()
            if calls:
                state=self.repo.load(app_id);state.llm_calls.extend(calls);state.record('agent_chat_completed',intent=intent);self.repo.save(state)
        return reply
    def _semantic_agent_intent(self,text,app_id,thread):
        current_job=None
        if app_id:
            state=self.repo.load(app_id);meta=self.meta(app_id)
            current=next((stage for stage in meta['recruitment_process'].get('stages',[])
                          if stage.get('stage_id')==meta['recruitment_process'].get('current_stage_id')),None)
            current_job={'company':state.company,'role':state.role,
                         'current_stage':current and current.get('name')}
        context={'user_message':text,'current_job':current_job,
                 'recent_messages':[{'role':item.get('role'),'content':item.get('content','')[:500]}
                                    for item in thread.get('messages',[])[-4:]]}
        executor=self.executor(self.provider,ProgressReporter(enabled=False))
        decision=executor.execute(self.loader.load('intent-router'),context,IntentDecision)
        if app_id and hasattr(executor,'drain_call_records'):
            calls=executor.drain_call_records()
            if calls:
                state=self.repo.load(app_id);state.llm_calls.extend(calls);state.record('agent_intent_classified',intent=decision.intent,confidence=decision.confidence);self.repo.save(state)
        return decision
    def agent_message(self,text,app_id=None):
        text=text.strip()
        if not text:raise ValueError('请输入消息。')
        if len(text)>12000:raise ValueError('单条消息不能超过 12000 字符。')
        if app_id:self.repo.load(app_id)
        with self.tasks.lock:
            thread=self.agent_thread(app_id)
            if thread.get('pending_proposal'):
                thread['pending_proposal']['status']='superseded'
                thread['pending_proposal']=None
            thread['messages'].append(agent_message_record('user',text))
            intent=route_explicit_intent(text,mode=thread['mode'])
            decision=None
            if intent is None:
                try:
                    decision=self._semantic_agent_intent(text,app_id,thread)
                    intent=reconcile_semantic_intent(text,decision.intent,has_current_job=bool(app_id))
                except Exception:
                    intent=reconcile_semantic_intent(
                        text,fallback_intent(text),has_current_job=bool(app_id)
                    )
            citations=self._agent_citations(text) if intent!='general_chat' else []
            pending=None;plan=[];suggested=[]
            state=self.repo.load(app_id) if app_id else None
            meta=self.meta(app_id) if app_id else None
            if decision is not None and decision.confidence < .70:
                intent='clarify_intent'
                content='我还不能确定你是在补充已经发生的招聘进展，还是希望针对当前岗位进行练习或咨询。请说明一下，我不会在确认前修改岗位数据。'
                suggested=['这是已经发生的进展','这是面试练习问题']
            elif intent=='update_progress':
                if not app_id:raise ValueError('请先进入对应岗位，再更新招聘进展。')
                parsed=parse_progress_note(text,meta['recruitment_process'])
                parts=[]
                if parsed.get('passed_stage_name'):parts.append('将 '+parsed['passed_stage_name']+' 标记为已通过')
                if parsed.get('next_stage_name'):parts.append('将当前阶段更新为 '+parsed['next_stage_name'])
                if parsed.get('risks'):parts.append('记录风险：'+'、'.join(parsed['risks']))
                plan=[{'tool':'update_recruitment_progress','reason':'保存轮次、反馈和风险'}]
                pending=agent_proposal('progress_update','；'.join(parts) or '保存本次招聘进展',parsed,plan)
                content='我识别到一次招聘进展更新。请先核对下面的变更；确认前不会修改岗位数据。'
                suggested=['补充面试官类型','补充面试时间']
            elif intent=='start_focused_training':
                if not app_id:raise ValueError('请先进入一个岗位，再开始专项训练。')
                dashboard=readiness(state,meta,self.session_list(app_id,validate=False));topic=select_topic(text,dashboard.get('risks'))
                training={'training_id':'training_'+uuid4().hex[:12],'topic':topic,'turns':[],
                          'question_index':0,'started_at':utc_now(),'evidence_ids':[c['evidence_id'] for c in citations]}
                thread['mode']='focused_training';thread['session_memory']['focused_training']=training
                question=TOPICS[topic]['questions'][0]
                content=f'已进入「{topic}专项训练」。我会提供提示和即时反馈，不按正式面试计分。\n\n第一题：{question}'
                plan=[{'tool':'search_evidence','reason':'查找与专项主题相关的真实经历'}]
                suggested=['结束训练']
            elif intent=='focused_training_answer':
                training=thread['session_memory'].get('focused_training')
                if not training:
                    thread['mode']='assistant';content='当前没有进行中的专项训练。你可以说“开始数据归因专项训练”。'
                else:
                    score,gaps=assess_training_answer(text);training['turns'].append({'answer':text,'score':score,'gaps':gaps,'at':utc_now()})
                    next_index=min(training['question_index']+1,len(TOPICS[training['topic']]['questions'])-1);training['question_index']=next_index
                    if gaps:
                        content=f'这次回答暂评 {score}/100。需要补充：'+ '；'.join(gaps)+f'\n\n继续追问：{TOPICS[training["topic"]]["questions"][next_index]}'
                    else:
                        content=f'这次回答暂评 {score}/100，事实、行动和判断依据比较完整。\n\n继续追问：{TOPICS[training["topic"]]["questions"][next_index]}'
                    suggested=['结束训练']
            elif intent=='finish_focused_training':
                training=thread['session_memory'].get('focused_training')
                if not training:content='当前没有进行中的专项训练。';thread['mode']='assistant'
                else:
                    scores=[turn['score'] for turn in training['turns']];average=round(sum(scores)/len(scores)) if scores else 0
                    payload={**training,'average_score':average,'completed_at':utc_now()}
                    plan=[{'tool':'write_training_result','reason':'保存训练结果并重新评估岗位风险'}]
                    pending=agent_proposal('complete_training',f'结束「{training["topic"]}」专项训练，平均表现 {average}/100',payload,plan)
                    content='专项训练已暂停。我可以在你确认后把结果写回当前岗位的风险和准备度。'
            elif intent=='save_evidence':
                if not app_id:raise ValueError('请先进入岗位，再保存职业证据。')
                fact=re.sub(r'^.*?(?:证据|经历|事实)[：:，,]?\s*','',text).strip() or text
                pending=agent_proposal('save_evidence','保存一条用户补充证据',{'text':fact},
                                       [{'tool':'save_user_evidence','reason':'写入用户级职业证据库并关联当前岗位'}])
                content='我把这段内容识别为新的职业事实。请核对原文，确认后才会写入证据库。'
            elif intent=='show_readiness':
                if not app_id:raise ValueError('请先进入对应岗位，再查看准备度。')
                dashboard=readiness(state,meta,self.session_list(app_id,validate=False));action=(dashboard.get('actions') or [{}])[0]
                content=f'当前岗位准备度为 {dashboard.get("overall",0)}%。'
                if dashboard.get('risks'):content+=' 主要风险：'+'、'.join(dashboard['risks'])+'。'
                if action.get('title'):content+=f' 建议下一步：{action["title"]}，因为{action.get("reason","")}'
                suggested=['开始专项训练','开始完整模拟面试']
            elif intent in {'analyze_jd','tailor_resume','generate_questions','prepare_answers'}:
                if not app_id:raise ValueError('请先进入对应岗位。')
                action={'analyze_jd':'analyze','tailor_resume':'resume','generate_questions':'questions','prepare_answers':'answers'}[intent]
                label={'analyze':'分析岗位','resume':'生成定制简历','questions':'生成面试问题','answers':'生成准备答案'}[action]
                plan=[{'tool':'search_evidence','reason':'检索与岗位及当前轮次相关的证据'},
                      {'tool':action,'reason':label},{'tool':'verify_evidence','reason':'校验证据引用'}]
                pending=agent_proposal('run_action',label,{'action':action},plan)
                content=f'我已经形成“{label}”执行计划。确认后将调用现有流水线，并保留运行记录和失败恢复点。'
            elif intent=='start_mock_interview':
                if not app_id:raise ValueError('请先进入一个岗位，再开始模拟面试。')
                if not state.interview_prep_profile:
                    plan=[{'tool':'questions','reason':'先生成当前轮次的问题清单'}]
                    pending=agent_proposal('run_action','先生成当前轮次问题，再开始完整模拟',{'action':'questions'},plan)
                    content='当前岗位还没有问题清单。确认后我会先生成当前轮次的问题，完成后即可开始模拟。'
                else:
                    plan=[{'tool':'start_mock_interview','reason':'创建一轮保留上下文快照的完整模拟'}]
                    pending=agent_proposal('start_mock_interview','开始一轮 3 道主问题的完整模拟面试',{'count':3},plan)
                    content='这是完整模拟：过程中减少提示，结束后统一复盘。确认后开始。'
            else:
                result=self._general_agent_reply(text,intent,app_id,citations)
                content=result.reply;suggested=result.suggested_prompts
                thread['mode']='general_chat' if intent=='general_chat' else 'assistant'
            assistant=agent_message_record('assistant',content,intent=intent,mode=thread['mode'],
                                           citations=citations,plan=plan,suggested_prompts=suggested,
                                           proposal_id=pending and pending['proposal_id'])
            thread['messages'].append(assistant);thread['pending_proposal']=pending
            self._save_agent_thread(thread)
        return {'thread':thread,'message':assistant,'proposal':pending}
    def _save_manual_evidence(self,app_id,text):
        evidence_id=stable_manual_evidence_id(text);store=self.evidence_store();now=utc_now()
        item=next((entry for entry in store.get('items',[]) if entry.get('evidence_id')==evidence_id),None)
        if not item:
            item={'evidence_id':evidence_id,'material_id':'manual_conversation','kind':'manual','source':'Agent 对话补充',
                  'source_version':1,'text':text,'confirmation_status':'confirmed','active':True,'created_at':now,'updated_at':now}
            store.setdefault('items',[]).append(item);write_json(self.evidence_path(),store)
        state=self.repo.load(app_id);evidence=copy.deepcopy(state.material_evidence or {})
        manual=evidence.setdefault('manual',[])
        if not any(entry.get('evidence_id')==evidence_id for entry in manual):manual.append(copy.deepcopy(item))
        state.material_evidence=evidence;state.record('manual_evidence_confirmed',evidence_id=evidence_id);self.repo.save(state)
        return item
    def confirm_agent_proposal(self,proposal_id,app_id=None):
        safe_id(proposal_id,'prop')
        with self.tasks.lock:
            thread=self.agent_thread(app_id);pending=thread.get('pending_proposal')
            if not pending or pending.get('proposal_id')!=proposal_id:raise ConflictError('这个操作已失效，请重新发送请求。')
            kind=pending['kind'];payload=pending['payload'];result=None
            if kind=='progress_update':result=self.confirm_progress(app_id,payload)
            elif kind=='run_action':result={'task':self.start_task(payload['action'],app_id)}
            elif kind=='start_mock_interview':result={'session':self.new_session(app_id,int(payload.get('count',3)))};thread['mode']='mock_interview'
            elif kind=='save_evidence':result={'evidence':self._save_manual_evidence(app_id,payload['text'])}
            elif kind=='complete_training':
                meta=self.meta(app_id);meta.setdefault('training_history',[]).append(payload)
                process=meta['recruitment_process'];current=next((s for s in process['stages'] if s.get('stage_id')==process.get('current_stage_id')),None)
                if current:
                    current.setdefault('risk_assessments',[]).append({'topic':payload['topic'],'score':payload['average_score'],'at':utc_now()})
                write_json(self.meta_path(app_id),meta);thread['mode']='assistant';thread['session_memory'].pop('focused_training',None)
                result={'training':payload}
            pending['status']='confirmed';pending['confirmed_at']=utc_now();thread['pending_proposal']=None
            result_text='操作已确认并执行。你可以继续在这里描述下一步目标。';citations=[];suggested=[]
            if kind=='progress_update':
                risks=payload.get('risks') or []
                citations=self._agent_citations('增长 数据归因 个人贡献')
                result_text='招聘进展已经保存。'+((' 当前需要优先修复：'+'、'.join(risks)+'。') if risks else '')
                result_text+=(' 我找到了一些相关职业证据，可以先进入专项训练验证表达。' if citations else ' 当前证据不足，建议先补充实验设计、数据口径和个人职责边界。')
                suggested=['开始数据归因专项训练','补充证据：']
            elif kind=='complete_training':
                result_text=f'「{payload["topic"]}」训练结果已写回岗位，准备度将使用本次 {payload["average_score"]}/100 的表现。'
                suggested=['开始完整模拟面试','查看当前准备度']
            elif kind=='save_evidence':
                result_text='新事实已作为用户确认的职业证据保存，并关联当前岗位。'
                suggested=['生成定制简历','开始专项训练']
            elif kind=='start_mock_interview':
                result_text='完整模拟面试已经创建。接下来进入面试官模式，整轮结束后统一复盘。'
            elif kind=='run_action':
                result_text='生成任务已经启动；它会保留运行日志，失败或中断后可以从当前步骤继续。'
            thread['messages'].append(agent_message_record('assistant',result_text,
                                                           intent='tool_result',mode=thread['mode'],tool_result={'kind':kind},
                                                           citations=citations,suggested_prompts=suggested))
            self._save_agent_thread(thread)
        return {'thread':thread,'result':result}
    def cancel_agent_proposal(self,proposal_id,app_id=None):
        safe_id(proposal_id,'prop')
        with self.tasks.lock:
            thread=self.agent_thread(app_id);pending=thread.get('pending_proposal')
            if not pending or pending.get('proposal_id')!=proposal_id:raise ConflictError('这个操作已失效。')
            pending['status']='cancelled';thread['pending_proposal']=None
            thread['messages'].append(agent_message_record('assistant','已取消，没有修改职业数据。',intent='proposal_cancelled',mode=thread['mode']))
            return self._save_agent_thread(thread)
    def confirm_evidence(self,evidence_id,status):
        if status not in {'confirmed','rejected','pending'}:raise ValueError('证据确认状态无效')
        with self.tasks.lock:
            store=self.evidence_store();item=next((i for i in store.get('items',[]) if i.get('evidence_id')==evidence_id),None)
            if not item:raise FileNotFoundError('找不到证据')
            item['confirmation_status']=status;item['updated_at']=utc_now();write_json(self.evidence_path(),store)
            for path in self.repo.directory.glob('job_*.json'):
                state=self.repo.load(path.stem);changed=False
                for entries in (state.material_evidence or {}).values():
                    if not isinstance(entries,list):continue
                    for evidence in entries:
                        if evidence.get('knowledge_id')==evidence_id:
                            evidence['confirmation_status']=status;changed=True
                if changed:
                    self.repo.save(state)
                    if status=='rejected':
                        meta=self.meta(state.application_id)
                        outputs=[name for name,value in (('定制简历',state.tailored_resume),('面试问题',state.interview_prep_profile),('准备答案',state.answer_prep_profile)) if value]
                        meta['material_impacts'].append({'change_id':'evidence_'+evidence_id,'status':'pending','created_at':utc_now(),
                            'message':'一条已引用证据被标记为不使用','affected_outputs':outputs or ['岗位材料快照']})
                        write_json(self.meta_path(state.application_id),meta)
        return item
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
        data['dashboard']=readiness(s,data['web'],self.session_list(app_id,validate=False))
        return data

    def save_stage(self,app_id,stage):
        with self.tasks.lock:
            self.tasks.idle(app_id);state=self.repo.load(app_id);meta=self.meta(app_id);process=meta['recruitment_process']
            stage=validate_stage(stage);stage_id=stage.get('stage_id')
            existing=next((item for item in process['stages'] if item.get('stage_id')==stage_id),None) if stage_id else None
            now=utc_now()
            if existing:
                existing.update(stage,updated_at=now);saved=existing
            else:
                saved={**stage,'stage_id':'stage_'+uuid4().hex[:12],
                       'sequence':int(stage.get('sequence') or len(process['stages'])+1),'created_at':now,'updated_at':now}
                process['stages'].append(saved)
            process['stages'].sort(key=lambda item:(item.get('sequence',999),item.get('created_at','')))
            if stage.get('make_current') or not process.get('current_stage_id'):process['current_stage_id']=saved['stage_id']
            meta['recruitment_process']=process;write_json(self.meta_path(app_id),meta)
            state.recruitment_process=copy.deepcopy(process)
            state.interview_prep_profile=None;state.answer_prep_profile=None;state.mock_interview_profile=None
            state.record('recruitment_stage_updated',stage_id=saved['stage_id'],name=saved['name'],status=saved['status'])
            self.repo.save(state)
        return self.get_app(app_id)

    def delete_stage(self,app_id,stage_id):
        safe_id(stage_id,'stage')
        with self.tasks.lock:
            self.tasks.idle(app_id);self.repo.load(app_id);meta=self.meta(app_id);process=meta['recruitment_process']
            before=len(process['stages']);process['stages']=[s for s in process['stages'] if s.get('stage_id')!=stage_id]
            if len(process['stages'])==before:raise FileNotFoundError('找不到招聘环节')
            if process.get('current_stage_id')==stage_id:process['current_stage_id']=None
            write_json(self.meta_path(app_id),meta)
        return self.get_app(app_id)

    def preview_progress(self,app_id,text):
        self.repo.load(app_id)
        return parse_progress_note(text,self.meta(app_id)['recruitment_process'])

    def confirm_progress(self,app_id,proposal):
        with self.tasks.lock:
            self.tasks.idle(app_id);state=self.repo.load(app_id);meta=self.meta(app_id);process=meta['recruitment_process'];now=utc_now()
            def find(name):return next((s for s in process['stages'] if s.get('name')==name),None)
            passed=None
            if proposal.get('passed_stage_name'):
                passed=find(proposal['passed_stage_name'])
                if not passed:
                    passed={'stage_id':'stage_'+uuid4().hex[:12],'sequence':len(process['stages'])+1,
                            'name':proposal['passed_stage_name'],'stage_type':'business','status':'passed',
                            'focus_areas':[],'risks':[],'feedback':[],'created_at':now,'updated_at':now}
                    process['stages'].append(passed)
                passed['status']='passed';passed['updated_at']=now
                passed.setdefault('feedback',[]).extend(x for x in proposal.get('feedback',[]) if x not in passed.get('feedback',[]))
                passed.setdefault('risks',[]).extend(x for x in proposal.get('risks',[]) if x not in passed.get('risks',[]))
            current=None
            if proposal.get('next_stage_name'):
                current=find(proposal['next_stage_name'])
                if not current:
                    current={'stage_id':'stage_'+uuid4().hex[:12],'sequence':len(process['stages'])+1,
                             'name':proposal['next_stage_name'],'stage_type':proposal.get('next_stage_type','business'),
                             'status':'scheduled' if proposal.get('scheduled_at') or proposal.get('scheduled_text') else 'planned',
                             'scheduled_at':proposal.get('scheduled_at'),'scheduled_text':proposal.get('scheduled_text'),'interviewer_role':None,
                             'focus_areas':[],'risks':list(proposal.get('risks',[])),'feedback':[],
                             'created_at':now,'updated_at':now}
                    process['stages'].append(current)
                else:current.setdefault('risks',[]).extend(x for x in proposal.get('risks',[]) if x not in current.get('risks',[]))
                process['current_stage_id']=current['stage_id']
            process['stages'].sort(key=lambda item:(item.get('sequence',999),item.get('created_at','')))
            meta['progress_notes'].append({'text':proposal.get('original_text',''),'saved_at':now})
            meta['recruitment_process']=process;write_json(self.meta_path(app_id),meta)
            state.recruitment_process=copy.deepcopy(process)
            state.interview_prep_profile=None;state.answer_prep_profile=None;state.mock_interview_profile=None
            state.record('recruitment_progress_confirmed',passed_stage_id=passed and passed['stage_id'],current_stage_id=current and current['stage_id'])
            self.repo.save(state)
        return self.get_app(app_id)
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
            version=max([int(m.get('version',1)) for m in self.materials() if m['kind']==kind and m.get('application_id')==app_id] or [0])+1
            destination=self.web/'uploads'/(mid+suffix);destination.parent.mkdir(parents=True,exist_ok=True)
            incoming=Path(temporary)
            try:incoming.replace(destination)
            except PermissionError:
                destination.write_bytes(incoming.read_bytes());incoming.unlink(missing_ok=True)
            item={'material_id':mid,'kind':kind,'name':Path(name.replace('\\','/')).name,'application_id':app_id,'version':version,
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
            portfolio_text='\n\n'.join('[SOURCE: '+m['name']+']\n'+m['text'] for m in portfolios)
            s=self.repo.load(app_id);s.update_materials(base_resume=resume['text'],portfolio=portfolio_text)
            built=build_material_evidence(base_resume=s.base_resume,portfolio=s.portfolio,jd_profile=s.jd_profile)
            known={(item.get('kind'),item.get('text')):item for item in self.evidence_store().get('items',[]) if item.get('active')}
            for group in ('resume','portfolio'):
                for item in built[group]:
                    match=known.get((group,item['text']))
                    if match:item.update(knowledge_id=match['evidence_id'],source_version=match.get('source_version',1),confirmation_status=match.get('confirmation_status','pending'))
            s.material_evidence=built;self.repo.save(s)
            meta=self.meta(app_id);meta['material_snapshot']={'bound_at':utc_now(),'resume':{'material_id':resume['material_id'],'name':resume['name']},
                'portfolio':[{'material_id':m['material_id'],'name':m['name']} for m in portfolios]}
            for impact in meta.get('material_impacts',[]):
                if impact.get('status')=='pending':impact['status']='resolved';impact['resolved_at']=utc_now()
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
    def session_list(self,app_id,validate=True):
        if validate:self.repo.load(app_id)
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
                previous=next((old for old in self.materials() if old['kind']==m['kind'] and old.get('application_id')==m.get('application_id') and old['active'] and old['material_id']!=m['material_id']),None)
                if m['kind']=='resume':
                    for old in self.materials():
                        if old['kind']=='resume' and old['active']:
                            old['active']=False;write_json(self.material_path(old['material_id']),old)
                write_json(self.material_path(m['material_id']),m)
                if m['kind'] in {'resume','portfolio'}:
                    store,change=ingest_evidence(self.evidence_store(),m,previous);write_json(self.evidence_path(),store)
                    if previous:
                        for state_path in self.repo.directory.glob('job_*.json'):
                            app_id=state_path.stem;meta=self.meta(app_id);snapshot=meta.get('material_snapshot') or {}
                            bound_ids={snapshot.get('resume',{}).get('material_id')}
                            bound_ids.update(item.get('material_id') for item in snapshot.get('portfolio',[]))
                            if previous['material_id'] not in bound_ids:continue
                            state=self.repo.load(app_id);outputs=[name for name,value in (
                                ('定制简历',state.tailored_resume),('面试问题',state.interview_prep_profile),('准备答案',state.answer_prep_profile)) if value]
                            meta['material_impacts'].append({'change_id':change['change_id'],'status':'pending','created_at':utc_now(),
                                'message':f"{m['name']} v{m['version']} 已更新",'affected_outputs':outputs or ['岗位材料快照']})
                            write_json(self.meta_path(app_id),meta)
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
