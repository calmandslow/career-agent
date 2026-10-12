from __future__ import annotations

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
except ImportError as exc:  # Clear failure before dependencies are installed.
    raise RuntimeError("Install API dependencies with: pip install -r requirements.txt") from exc

from career_agent import ApplicationState, Intent, JsonStateRepository, Orchestrator, make_plan
from career_agent.executor_factory import create_executor
from career_agent.repository import StateNotFoundError
from career_agent.skill_loader import SkillLoader


app = FastAPI(title="Career Agent V1", version="1.7.1")
repository = JsonStateRepository()


class CreateApplicationRequest(BaseModel):
    company: str | None = None
    role: str | None = None


class MaterialsRequest(BaseModel):
    jd: str | None = None
    base_resume: str | None = None
    portfolio: str | None = None
    interview_record: str | None = None


class PlanRequest(BaseModel):
    intent: Intent


class RunRequest(BaseModel):
    intent: Intent


def get_state(application_id: str) -> ApplicationState:
    try:
        return repository.load(application_id)
    except (StateNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Application not found") from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/applications")
def create_application(body: CreateApplicationRequest) -> dict:
    state = ApplicationState(company=body.company, role=body.role)
    state.record("application_created")
    repository.save(state)
    return state.to_dict()


@app.get("/applications/{application_id}")
def read_application(application_id: str) -> dict:
    return get_state(application_id).to_dict()


@app.patch("/applications/{application_id}/materials")
def update_materials(application_id: str, body: MaterialsRequest) -> dict:
    state = get_state(application_id)
    state.update_materials(**body.model_dump())
    repository.save(state)
    return state.to_dict()


@app.post("/applications/{application_id}/plan")
def create_plan(application_id: str, body: PlanRequest) -> dict:
    state = get_state(application_id)
    plan = make_plan(body.intent, state)
    state.record("plan_created", intent=body.intent.value, steps=[step.to_dict() for step in plan])
    repository.save(state)
    return {"application_id": application_id, "intent": body.intent.value, "steps": [step.to_dict() for step in plan]}


@app.post("/applications/{application_id}/run")
def run_application(application_id: str, body: RunRequest) -> dict:
    get_state(application_id)
    try:
        orchestrator = Orchestrator(repository, SkillLoader(), create_executor())
        return orchestrator.run(application_id, body.intent).to_dict()
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
