from __future__ import annotations

from .answer_policy import infer_answer_mode

import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .context_builder import build_context
from .evidence_builder import build_material_evidence
from .executor import SkillExecutor
from .models import ApplicationState, Intent, StepType, utc_now
from .planner import make_plan
from .repository import JsonStateRepository
from .resume_renderer import render_resume_markdown
from .resume_truthfulness import (
    CONSERVATIVE_REPAIR_INSTRUCTION,
    apply_conservative_resume_framing,
)
from .schemas import OUTPUT_MODELS, PreparedAnswer, ResumeSemanticReview
from .semantic_verifier import (
    SemanticVerificationError,
    build_resume_semantic_context,
    verify_resume_semantic_review,
)
from .skill_loader import SkillLoader
from .verifier import AnswerVerificationError, VerificationError, verify_skill_output
from .telemetry import ProgressReporter


@dataclass(slots=True)
class RunResult:
    status: str
    application_id: str
    completed_steps: list[str]
    pending_question: dict[str, str] | None = None
    run_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "application_id": self.application_id,
            "completed_steps": self.completed_steps,
            "pending_question": self.pending_question,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class Orchestrator:
    def __init__(
        self,
        repository: JsonStateRepository,
        loader: SkillLoader,
        executor: SkillExecutor,
        progress: ProgressReporter | None = None,
    ) -> None:
        self.repository = repository
        self.loader = loader
        self.executor = executor
        self.progress = progress or getattr(executor, "progress", None) or ProgressReporter()
        self.last_run_id: str | None = None
        self.last_started_at: str | None = None

    def run(self, application_id: str, intent: Intent) -> RunResult:
        run_id = uuid4().hex[:12]
        self.last_run_id = run_id
        started_at = utc_now()
        self.last_started_at = started_at
        set_run_id = getattr(self.executor, "set_run_id", None)
        if callable(set_run_id):
            set_run_id(run_id)
        state = self.repository.load(application_id)
        plan = make_plan(intent, state)
        completed: list[str] = []
        total_skills = sum(1 for step in plan if step.type is StepType.SKILL)
        skill_index = 0
        state.record(
            "run_started",
            run_id=run_id,
            intent=intent.value,
            planned_skills=total_skills,
        )
        self.repository.save(state)
        self.progress.emit(
            f"[Run {run_id}] started · {total_skills} pending skill(s) · intent={intent.value}"
        )

        for step in plan:
            if step.type is StepType.ASK_USER:
                state.current_stage = f"waiting_for_{step.field}"
                state.record("run_paused", field=step.field, reason=step.reason)
                self.repository.save(state)
                self.progress.emit(f"[Run {run_id}] paused · waiting for {step.field}")
                return RunResult(
                    status="waiting_for_user",
                    application_id=application_id,
                    completed_steps=completed,
                    pending_question={"field": step.field or "", "reason": step.reason or ""},
                    run_id=run_id,
                    started_at=started_at,
                    finished_at=utc_now(),
                )

            if step.type is not StepType.SKILL or not step.name:
                raise RuntimeError(f"Unsupported plan step: {step.to_dict()}")
            if step.name not in OUTPUT_MODELS:
                raise RuntimeError(f"Skill execution not implemented in this milestone: {step.name}")

            skill_index += 1
            skill_started = time.monotonic()
            self.progress.emit(f"[{skill_index}/{total_skills}] {step.name} started")
            state.current_stage = f"running_{step.name}"
            state.record("skill_started", skill=step.name, run_id=run_id)
            self.repository.save(state)

            skill = self.loader.load(step.name)
            if step.name == "resume-tailor" and state.material_evidence is None:
                state.material_evidence = build_material_evidence(
                    base_resume=state.base_resume,
                    portfolio=state.portfolio,
                    jd_profile=state.jd_profile,
                )
                state.record("material_evidence_built", **state.material_evidence["stats"])
                self.repository.save(state)
            context = build_context(step.name, state)
            try:
                output = self._execute(skill, context, OUTPUT_MODELS[step.name], state)
                try:
                    self._verify_output(step.name, output, context, state)
                except VerificationError as first_error:
                    if isinstance(first_error, SemanticVerificationError) and not first_error.repairable:
                        return self._pause_for_validation(
                            state=state,
                            skill_name=step.name,
                            output=output,
                            issues=[str(first_error)],
                            completed=completed,
                        )
                    state.record(
                        "skill_validation_retry",
                        skill=step.name,
                        error=str(first_error),
                        run_id=run_id,
                    )
                    self.progress.emit(
                        f"[Verifier] {step.name} requires one repair · {type(first_error).__name__}"
                    )
                    self.repository.save(state)
                    if step.name == "interview-answer-coach" and isinstance(
                        first_error, AnswerVerificationError
                    ):
                        output = self._repair_one_answer(
                            skill, context, output, first_error, state
                        )
                    else:
                        repair_context = {
                            **context,
                            "verification_feedback": str(first_error),
                            "previous_output": output.model_dump(),
                            "repair_instruction": (
                                "Repair the previous output only. Preserve supported facts, correct the "
                                "reported validation problem, and return the complete schema again.\n\n"
                                + CONSERVATIVE_REPAIR_INSTRUCTION
                            ),
                        }
                        output = self._execute(
                            skill,
                            repair_context,
                            OUTPUT_MODELS[step.name],
                            state,
                        )
                    if isinstance(first_error, SemanticVerificationError):
                        output = apply_conservative_resume_framing(output)
                    try:
                        self._verify_output(
                            step.name,
                            output,
                            context,
                            state,
                            force_all_semantic_claims=isinstance(
                                first_error, SemanticVerificationError
                            ),
                        )
                    except VerificationError as final_error:
                        return self._pause_for_validation(
                            state=state,
                            skill_name=step.name,
                            output=output,
                            issues=[str(final_error)],
                            completed=completed,
                        )
            except Exception as exc:
                state.current_stage = f"failed_{step.name}"
                state.record(
                    "skill_failed",
                    skill=step.name,
                    error_type=type(exc).__name__,
                    run_id=run_id,
                )
                self.repository.save(state)
                self.progress.emit(
                    f"[{skill_index}/{total_skills}] {step.name} failed · {type(exc).__name__}"
                )
                raise
            self._apply_output(step.name, output.model_dump(), state)
            state.pending_validation_output = None
            state.validation_issues = None
            duration = round(time.monotonic() - skill_started, 3)
            state.record(
                "skill_completed",
                skill=step.name,
                run_id=run_id,
                duration_seconds=duration,
            )
            self.repository.save(state)
            completed.append(step.name)
            self.progress.emit(
                f"[{skill_index}/{total_skills}] {step.name} completed · {duration:.1f}s"
            )

        state.current_stage = "completed"
        state.record("goal_completed", intent=intent.value, run_id=run_id)
        self.repository.save(state)
        finished_at = utc_now()
        self.progress.emit(f"[Run {run_id}] completed")
        return RunResult(
            "completed",
            application_id,
            completed,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _execute(self, skill, context, output_model, state: ApplicationState):
        try:
            return self.executor.execute(skill, context, output_model)
        finally:
            self._persist_call_records(state)

    def _persist_call_records(self, state: ApplicationState) -> None:
        drain = getattr(self.executor, "drain_call_records", None)
        if not callable(drain):
            return
        records = drain()
        if not records:
            return
        for record in records:
            if not record.get("run_id"):
                record["run_id"] = self.last_run_id
            state.llm_calls.append(record)
            state.record(
                "llm_call_recorded",
                run_id=record.get("run_id"),
                skill=record.get("skill"),
                model=record.get("model"),
                call_type=record.get("call_type"),
                attempt=record.get("attempt"),
                status=record.get("status"),
                input_tokens=record.get("input_tokens"),
                output_tokens=record.get("output_tokens"),
                duration_seconds=record.get("duration_seconds"),
                error_type=record.get("error_type"),
                error_category=record.get("error_category"),
                retryable=record.get("retryable"),
            )
        self.repository.save(state)

    def _repair_one_answer(
        self,
        skill,
        context: dict[str, Any],
        output,
        error: AnswerVerificationError,
        state: ApplicationState,
    ):
        """Repair only the failing answer and preserve every other batch item."""
        original = output.model_dump()
        target = original["answers"][error.answer_index]
        cited = set(target.get("evidence_to_use", []))
        candidate_evidence = context.get("candidate_evidence") or {}
        focused_evidence = {
            source: [
                item for item in items
                if isinstance(item, dict) and item.get("evidence_id") in cited
            ]
            for source, items in candidate_evidence.items()
            if isinstance(items, list)
        }
        repair_context = {
            "target_question": target["question"],
            "previous_answer": target,
            "question_intent": infer_answer_mode(target["question"]),
            "candidate_evidence": focused_evidence,
            "jd_profile": context.get("jd_profile"),
            "verification_feedback": str(error),
            "repair_instruction": (
                "Return ONE PreparedAnswer only. Repair only this question. Preserve supported facts "
                "and evidence IDs. Do not add unsupported time promises. For hypothetical product "
                "scenarios, investigate business goals, users, pain points and constraints before "
                "proposing a solution. For ABTest, cover randomization/exposure, core and guardrail "
                "metrics, SRM or contamination checks, experiment duration, confidence interval and "
                "practical significance."
            ),
        }
        repaired = self._execute(skill, repair_context, PreparedAnswer, state)
        if repaired.question != target["question"]:
            raise AnswerVerificationError(
                "single-answer repair changed the source question",
                answer_index=error.answer_index,
                code="changed_question",
            )
        original["answers"][error.answer_index] = repaired.model_dump()
        return type(output).model_validate(original)

    def _verify_output(
        self,
        skill_name: str,
        output,
        context: dict[str, Any],
        state: ApplicationState,
        *,
        force_all_semantic_claims: bool = False,
    ) -> None:
        semantic_answers = skill_name == "interview-answer-coach" and getattr(self.executor, "answer_semantic_enabled", False)
        verify_skill_output(skill_name, output, context={**context, "answer_hard_only": semantic_answers})
        if semantic_answers:
            from .answer_semantic import review_answer, review_is_pass, review_key, has_passed_review
            for index, answer in enumerate(output.answers):
                cached = has_passed_review(state, answer)
                if cached:
                    continue
                try:
                    review = review_answer(self.executor, answer, context)
                finally:
                    self._persist_call_records(state)
                state.record("answer_semantic_review", answer_index=index, answer_hash=review_key(answer, state), review=review.model_dump())
                self.repository.save(state)
                if not review_is_pass(review):
                    if review.verdict == "human_review":
                        raise SemanticVerificationError("Answer requires factual clarification", repairable=False)
                    raise AnswerVerificationError(str(review.model_dump()), answer_index=index, code="semantic_answer")
                state.record("answer_semantic_passed", answer_index=index, answer_hash=review_key(answer, state))
            return
        if skill_name != "resume-tailor":
            return

        semantic_context = build_resume_semantic_context(
            output,
            context,
            force_all_resume_claims=force_all_semantic_claims,
        )

        review_skill = self.loader.load("resume-claim-verifier")
        self.progress.emit(
            f"[Verifier] semantic review started · {len(semantic_context['claims_to_review'])} claim(s)"
        )
        review = self._execute(
            review_skill,
            semantic_context,
            ResumeSemanticReview,
            state,
        )
        verdict_counts: dict[str, int] = {}
        for item in review.reviews:
            verdict_counts[item.verdict] = verdict_counts.get(item.verdict, 0) + 1
        result = {
            "status": "completed",
            **review.model_dump(),
        }
        state.resume_semantic_review = result
        state.resume_semantic_review_history.append(result)
        state.record(
            "resume_semantic_reviewed",
            claim_count=len(review.reviews),
            verdict_counts=verdict_counts,
            run_id=self.last_run_id,
        )
        self.progress.emit(
            f"[Verifier] semantic review completed · {verdict_counts}"
        )
        verify_resume_semantic_review(review, semantic_context)

    def _pause_for_validation(
        self,
        *,
        state: ApplicationState,
        skill_name: str,
        output,
        issues: list[str],
        completed: list[str],
    ) -> RunResult:
        state.current_stage = f"waiting_for_human_review_{skill_name}"
        state.pending_validation_output = output.model_dump()
        state.validation_issues = issues
        state.record(
            "run_paused_for_validation",
            skill=skill_name,
            error=issues[-1],
            run_id=self.last_run_id,
        )
        self.repository.save(state)
        self.progress.emit(f"[Run {self.last_run_id}] paused · human review required")
        return RunResult(
            status="waiting_for_user",
            application_id=state.application_id,
            completed_steps=completed,
            pending_question={
                "field": "human_review",
                "reason": issues[-1],
            },
            run_id=self.last_run_id,
            started_at=self.last_started_at,
            finished_at=utc_now(),
        )

    @staticmethod
    def _apply_output(skill_name: str, output: dict[str, Any], state: ApplicationState) -> None:
        if skill_name == "jd-analyzer":
            state.jd_profile = output
            state.company = state.company or output.get("company")
            state.role = state.role or output.get("role_title")
            return
        if skill_name == "resume-tailor":
            state.tailored_resume_structured = output["tailored_resume"]
            state.tailored_resume = render_resume_markdown(output["tailored_resume"])
            state.candidate_role_profile = output["candidate_role_profile"]
            state.resume_evidence = output["resume_evidence"]
            state.resume_claim_audit = output["claim_audit"]
            state.resume_change_notes = output["change_notes"]
            return
        field_by_skill = {
            "interview-research": "interview_research_profile",
            "interview-question-generator": "interview_prep_profile",
            "interview-answer-coach": "answer_prep_profile",
            "mock-interview": "mock_interview_profile",
            "interview-review": "interview_review_profile",
        }
        if skill_name in field_by_skill:
            setattr(state, field_by_skill[skill_name], output)
            return
        raise RuntimeError(f"No state mapper registered for skill: {skill_name}")
