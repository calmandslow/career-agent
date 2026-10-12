"""Same-origin localhost HTTP API and frontend. Start with python run_web.py."""
from contextlib import asynccontextmanager
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from career_agent.web_service import WebService, ConflictError
from career_agent.model_router import ModelRouter
from career_agent.schemas import JDProfile

ROOT=Path(__file__).resolve().parent
class CreateJob(BaseModel):
    company:str=Field(default='',max_length=200)
    role:str=Field(default='',max_length=200)
class Draft(BaseModel):text:str=Field(min_length=1,max_length=100000)
class AnswerConfirmation(BaseModel):
    question_index:int=Field(ge=0)
    answer_hash:str=Field(min_length=64,max_length=64)
class Action(BaseModel):
    action:str
    question_index:int|None=None
    feedback:str=Field(default='',max_length=6000)
class SessionRequest(BaseModel):count:int=Field(default=3,ge=1,le=10)
class InterviewAction(BaseModel):
    operation:str
    answer:str=Field(default='',max_length=12000)


def create_app(root=ROOT,provider='qwen',service=None):
    svc=service or WebService(root,provider)
    @asynccontextmanager
    async def lifespan(app):
        yield
        svc.tasks.close()
    app=FastAPI(title='Career Agent 本地工作台',version='1.8.6.1',lifespan=lifespan)
    app.state.service=svc
    @app.middleware('http')
    async def local_only(request,call_next):
        # This single-user server deliberately has no remote hosting/auth flow.
        host=request.url.hostname
        if host not in {'127.0.0.1','localhost','::1','testserver'}:
            return JSONResponse({'detail':'本地工作台仅接受本机访问。'},status_code=403)
        origin=request.headers.get('origin')
        if origin and origin!=str(request.base_url).rstrip('/'):
            return JSONResponse({'detail':'不允许跨站访问。请通过本地工作台页面操作。'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response
    @app.exception_handler(ConflictError)
    async def conflict(request,exc):return JSONResponse({'detail':str(exc)},status_code=409)
    @app.exception_handler(FileNotFoundError)
    async def not_found(request,exc):return JSONResponse({'detail':'未找到记录，请确认已迁移旧版状态。'},status_code=404)
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'detail':str(exc)[:700]},status_code=422)
    @app.get('/api/config')
    def config():
        router=ModelRouter.from_qwen_env()
        return {'version':'1.8.6.1','provider':svc.provider,'api_key_configured':bool(os.getenv('DASHSCOPE_API_KEY')),
                'routes':{'fast':router.fast_model,'core':router.core_model,'review':router.model_for('answer-semantic-verifier')},
                'max_file_mb':25,'pricing_note':'费用按本地配置估算，免费额度、未知用量未自动扣除；实际以控制台为准。'}
    @app.get('/api/materials')
    def materials():return svc.materials()
    @app.post('/api/materials')
    async def upload(file:UploadFile=File(...),kind:str=Form(...),application_id:str|None=Form(None)):
        temp_root=svc.web/'incoming';temp_root.mkdir(parents=True,exist_ok=True)
        fd,path=tempfile.mkstemp(dir=temp_root);os.close(fd)
        try:
            total=0
            with open(path,'wb') as stream:
                while chunk:=await file.read(1024*1024):
                    total+=len(chunk)
                    if total>25*1024*1024:raise ValueError('单个文件不能超过 25 MB。')
                    stream.write(chunk)
            if not total:raise ValueError('文件为空。')
            return svc.register_file(kind,file.filename or 'material',path,application_id)
        finally:
            Path(path).unlink(missing_ok=True);await file.close()
    @app.post('/api/materials/{mid}/deactivate')
    def deactivate(mid:str):return svc.deactivate(mid)
    @app.get('/api/applications')
    def applications():return svc.list_apps()
    @app.post('/api/applications')
    def create(body:CreateJob):return svc.create_app(body.company,body.role)
    @app.get('/api/deleted-applications')
    def deleted():return svc.deleted_apps()
    @app.delete('/api/applications/{aid}')
    def delete(aid:str):return svc.delete_app(aid)
    @app.post('/api/deleted-applications/{aid}/restore')
    def restore(aid:str):return svc.restore_app(aid)
    @app.get('/api/applications/{aid}')
    def application(aid:str):return svc.get_app(aid)
    @app.patch('/api/applications/{aid}')
    def patch(aid:str,body:CreateJob):return svc.patch_app(aid,body.company,body.role)
    @app.put('/api/applications/{aid}/jd-profile')
    def jd_profile(aid:str,body:JDProfile):return svc.save_jd_profile(aid,body)
    @app.post('/api/applications/{aid}/bind-materials')
    def bind(aid:str):return svc.bind_materials(aid)
    @app.put('/api/applications/{aid}/resume-draft')
    def draft(aid:str,body:Draft):return svc.save_draft(aid,body.text)
    @app.post('/api/applications/{aid}/confirm-answer')
    def confirm_answer(aid:str,body:AnswerConfirmation):return svc.confirm_answer(aid,body.question_index,body.answer_hash)
    @app.post('/api/applications/{aid}/approve')
    def approve(aid:str):return svc.approve(aid)
    @app.post('/api/applications/{aid}/tasks',status_code=202)
    def task(aid:str,body:Action):
        if body.action=='update_answer' and (body.question_index is None or not body.feedback.strip()):raise ValueError('请选择答案并填写修改反馈。')
        return svc.start_task(body.action,aid,body.model_dump(exclude={'action'},exclude_none=True))
    @app.get('/api/tasks')
    def tasks(application_id:str|None=None):return svc.tasks.list(application_id)
    @app.get('/api/tasks/{tid}')
    def task_status(tid:str):return svc.tasks.get(tid)
    @app.post('/api/tasks/{tid}/retry',status_code=202)
    def retry(tid:str):return svc.tasks.retry(tid)
    @app.get('/api/applications/{aid}/sessions')
    def sessions(aid:str):return svc.session_list(aid)
    @app.post('/api/applications/{aid}/sessions')
    def new_session(aid:str,body:SessionRequest):return svc.new_session(aid,body.count)
    @app.get('/api/applications/{aid}/sessions/{sid}')
    def session(aid:str,sid:str):return svc.session(aid,sid)
    @app.post('/api/applications/{aid}/sessions/{sid}/actions',status_code=202)
    def interview(aid:str,sid:str,body:InterviewAction):return svc.start_task('interview',aid,{**body.model_dump(),'session_id':sid})
    @app.get('/api/applications/{aid}/download/{kind}')
    def download(aid:str,kind:str,session_id:str|None=None):
        text,name=svc.download(aid,kind,session_id)
        if not text:raise ValueError('还没有可下载的内容。')
        return Response(text,media_type='text/plain; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{name}"'})
    @app.get('/')
    def index():return FileResponse(ROOT/'web'/'index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static')
    return app
