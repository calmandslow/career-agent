"""Persisted turn-by-turn interview engine, shared by CLI and a future UI."""
from __future__ import annotations
import json
import re
import time
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, Field, create_model
from .models import utc_now
from .interview_coach import CoachFeedback, CoachItem, command_input, explicitly_unable, annotate_legacy, validate_coaching, unevaluated
from .skill_loader import LoadedSkill

class FollowDecision(BaseModel):
    action: Literal['follow_up', 'next']
    reason: Literal['evidence_gap', 'unclear', 'ownership', 'contradiction', 'tradeoff', 'extension', 'sufficient', 'unable', 'not_probed', 'skipped']
    answer_quality: Literal['complete', 'needs_more', 'unable', 'not_evaluated'] = 'not_evaluated'
    follow_up_kind: Literal['supplement', 'extension', 'none'] = 'none'
    rationale: str = ''
    answer_quote: str = ''
    question: str = ''

class FollowDecisionError(ValueError):
    """A model decision failed validation, not a judgment on the user's answer."""

def validate_follow_decision(decision, turn):
    decision = decision.model_copy(deep=True)
    if decision.action == 'next':
        decision.follow_up_kind = 'none'
        if decision.answer_quality == 'not_evaluated' and decision.reason == 'sufficient':
            decision.answer_quality = 'complete'
        return decision
    quote = decision.answer_quote.strip()
    if not decision.question.strip() or not quote:
        raise FollowDecisionError('追问必须引用你实际回答中的原文，并给出具体问题。')
    if quote not in turn['answer']:
        # Formatting changes are not fabricated evidence. Match only whitespace
        # runs; words, punctuation, numbers and negation must remain exact.
        match = re.search(r'\s+'.join(re.escape(part) for part in quote.split()), turn['answer'])
        if not match:
            raise FollowDecisionError('追问引用与实际回答不一致；仅允许连续空格、换行等空白格式差异。请引用原回答中的短句。')
        quote = match.group(0)
    decision.answer_quote = quote  # Save the exact source substring, not a rewrite.
    if decision.reason in {'unable', 'not_probed', 'skipped'} or decision.answer_quality == 'unable':
        raise FollowDecisionError('追问原因与继续追问的动作不一致；明确不会回答或跳过时应进入下一题。')
    if decision.follow_up_kind == 'none':
        # Backward-compatible defaults for older fixtures and decisions. A
        # sufficient answer can lead to an extension, never an automatic veto.
        decision.follow_up_kind = 'extension' if decision.reason in {'sufficient', 'extension'} else 'supplement'
    if decision.follow_up_kind == 'extension' and not decision.rationale.strip():
        raise FollowDecisionError('延伸追问需要说明要继续考察的角度；回答充分本身不是禁止追问的理由。')
    return decision

class DimensionFeedback(BaseModel):
    dimension: str
    score: int = Field(ge=1, le=5)
    turn_ids: list[int] = Field(min_length=1)
    observation: str
    improvement: str

class SessionFeedback(BaseModel):
    overall_assessment: str
    scorecard: list[DimensionFeedback] = Field(min_length=1)
    strengths: list[str]
    weaknesses: list[str]
    evidence_gaps: list[str]
    repractice_questions: list[str]
    next_actions: list[str] = Field(min_length=1, max_length=6)

class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
    def path(self, session_id):
        if not re.fullmatch(r'mock_[a-f0-9]{12}', session_id):
            raise ValueError('Invalid session ID')
        return self.root / f'{session_id}.json'
    def save(self, session):
        with self._lock:
            path = self.path(session['session_id'])
            tmp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
            tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
            for attempt in range(4):
                try:
                    tmp.replace(path)
                    break
                except PermissionError:
                    if attempt == 3:
                        path.write_bytes(tmp.read_bytes())
                        tmp.unlink(missing_ok=True)
                        break
                    time.sleep(.01 * (attempt + 1))
    def load(self, session_id):
        with self._lock:
            path=self.path(session_id)
            for attempt in range(4):
                try:return json.loads(path.read_text(encoding='utf-8'))
                except PermissionError:
                    if attempt==3:raise
                    time.sleep(.01*(attempt+1))
    def latest(self, application_id, provider):
        sessions = [json.loads(p.read_text(encoding='utf-8')) for p in self.root.glob('mock_*.json')]
        matching = [s for s in sessions if s['application_id'] == application_id and s['provider'] == provider]
        return max(matching, key=lambda s: s['created_at']) if matching else None


def start_session(state, *, count=3, provider='qwen'):
    if not 1 <= count <= 10:
        raise ValueError('Main question count must be 1..10')
    items = (state.interview_prep_profile or {}).get('questions') or []
    if not items:
        raise ValueError('Saved State needs interview questions; generate the question bank first')
    indexed = sorted(enumerate(items), key=lambda x: (str(x[1].get('priority', 'P1')), x[0]))
    questions = []
    for _, item in indexed:
        text = item.get('question', '').strip()
        if text and text not in questions: questions.append(text)
    if not questions:
        raise ValueError('Question bank is empty')
    # Snapshot only compact evidence. Do not use prepared answers as grading truth.
    evidence = {}
    remaining = 12000
    truncated = False
    for key, entries in (state.material_evidence or {}).items():
        if not isinstance(entries, list): continue
        evidence[key] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('confirmation_status')=='rejected': continue
            size = len(json.dumps(entry, ensure_ascii=False))
            if size > remaining:
                truncated = True
                continue
            evidence[key].append(entry)
            remaining -= size
    return {'version': '1.7.2.2', 'session_id': 'mock_' + uuid4().hex[:12],
            'application_id': state.application_id, 'provider': provider, 'created_at': utc_now(),
            'jd_profile': state.jd_profile, 'evidence': evidence, 'evidence_truncated': truncated,
            'recruitment_process': state.recruitment_process,
            'questions': questions[:count], 'main_index': 0, 'phase': 'awaiting_answer',
            'current_kind': 'main', 'current_question': questions[0], 'turns': [],
            'decisions': [], 'feedback': None, 'llm_calls': []}

class InterviewEngine:
    def __init__(self, store, session, executor_factory):
        self.store, self.session, self.executor_factory = store, session, executor_factory
        self.executor = None
    def submit(self, answer):
        s = self.session
        if s['phase'] != 'awaiting_answer':
            raise ValueError('Process the pending step before submitting another answer')
        if command_input(answer): raise ValueError('Control commands are not interview answers')
        if not answer.strip(): raise ValueError('Answer must not be empty')
        if len(answer) > 12000: raise ValueError('Answer exceeds 12000 characters')
        s['turns'].append({'turn_id': len(s['turns']) + 1, 'main_index': s['main_index'],
            'kind': s['current_kind'], 'question': s['current_question'], 'answer': answer.strip(),
            'response_status': 'unable' if explicitly_unable(answer) else 'answered', 'answered_at': utc_now()})
        s['phase'] = 'evaluate' if s['current_kind'] == 'main' else 'advance'
        self.store.save(s)  # Save user input BEFORE a potentially failing model call.
    def skip(self):
        self.submit('我暂时无法回答这道题。')
        self.session['turns'][-1]['skipped'] = True
        self.session['turns'][-1]['response_status'] = 'skipped'
        self.session['phase'] = 'advance'
        self.store.save(self.session)
    def redo_feedback(self):
        s = self.session
        if not s['turns']: raise ValueError('No saved responses to coach')
        if s['phase'] not in {'completed','summary'}: raise ValueError('Finish the interview before regenerating feedback')
        annotate_legacy(s)
        if not s.get('feedback_revision_pending'):
            s.setdefault('feedback_history', []).append({'feedback':s.get('feedback'), 'created_at':utc_now(),
                'policy_version':s.get('feedback_policy_version', '1.7.2')})
            s['feedback_revision_pending'] = True
            s.pop('feedback_candidate', None)
        s['phase'] = 'summary'
        self.store.save(s)
    def finish_early(self):
        if not self.session['turns']: raise ValueError('Answer at least one question before finishing')
        if self.session['phase'] == 'completed': return
        self.session['phase'] = 'summary'
        self.session['ended_early'] = True
        self.store.save(self.session)
    def move_on(self):
        """Keep the submitted answer, explicitly omit the optional follow-up."""
        s = self.session
        if s['phase'] != 'evaluate':
            raise ValueError('只有已提交主问题、等待追问判断时才能保留回答进入下一题。')
        s['decisions'].append({'main_index': s['main_index'], 'turn_id': s['turns'][-1]['turn_id'],
            **FollowDecision(action='next', reason='not_probed', rationale='用户选择保留回答并进入下一题。').model_dump(),
            'source': 'user'})
        s.pop('decision_candidate', None)
        s['phase'] = 'advance'
        self.store.save(s)
    def _call(self, policy, context, model):
        if self.executor is None:
            self.executor = self.executor_factory()
            setter = getattr(self.executor, 'set_run_id', None)
            if setter: setter(self.session['session_id'])
        instructions = (Path(__file__).resolve().parents[1] / 'config' / policy).read_text(encoding='utf-8')
        try:
            return self.executor.execute(LoadedSkill('mock-interview', instructions), context, model)
        finally:
            drain = getattr(self.executor, 'drain_call_records', None)
            if drain:
                self.session['llm_calls'].extend(drain())
            self.store.save(self.session)
    def _decide(self, turn):
        s = self.session
        context = {'jd_profile': s['jd_profile'], 'recruitment_process': s.get('recruitment_process'), 'main_question': s['questions'][s['main_index']],
                   'candidate_answer': turn['answer']}
        cached = s.get('decision_candidate')
        if not cached or cached.get('turn_id') != turn['turn_id']:
            output = self._call('mock_interview_turn.md', context, FollowDecision)
            cached = {'turn_id': turn['turn_id'], 'output': output.model_dump(), 'repair_count': 0}
            s['decision_candidate'] = cached
            self.store.save(s)
        while True:
            output = FollowDecision.model_validate(cached['output'])
            try:
                decision = validate_follow_decision(output, turn)
            except FollowDecisionError as exc:
                issue = str(exc)
                if not cached.get('recorded'):
                    s.setdefault('decision_attempts', []).append({'turn_id': turn['turn_id'],
                        'main_index': s['main_index'], 'output': cached['output'], 'accepted': False,
                        'validation_issue': issue, 'repair_count': cached['repair_count'], 'created_at': utc_now()})
                    cached['recorded'] = True
                    self.store.save(s)
                if cached['repair_count'] >= 1:
                    raise FollowDecisionError('追问判断经一次修正仍未通过检查。你的回答已保存，可保留回答进入下一题，或结束并生成反馈。') from exc
                progress = getattr(self.executor, 'progress', None)
                if progress:
                    progress.emit('[Verifier] 追问决策未通过检查，正在进行一次定向修正。')
                output = self._call('mock_interview_turn.md', {**context,
                    'repair': {'previous_decision': cached['output'], 'validation_issue': issue,
                               'instruction': '只修正当前决策。回答充分仍可做有明确目的的延伸追问，不得伪造回答缺陷或引用。'}}, FollowDecision)
                cached = {'turn_id': turn['turn_id'], 'output': output.model_dump(), 'repair_count': 1}
                s['decision_candidate'] = cached
                self.store.save(s)
                continue
            s.setdefault('decision_attempts', []).append({'turn_id': turn['turn_id'],
                'main_index': s['main_index'], 'output': cached['output'], 'accepted': True,
                'validation_issue': None, 'repair_count': cached['repair_count'], 'created_at': utc_now()})
            s.pop('decision_candidate', None)
            s['turn_policy_version'] = '1.8.6'
            progress = getattr(self.executor, 'progress', None)
            if progress and decision.action == 'follow_up':
                progress.emit('[Interview] '+('延伸追问' if decision.follow_up_kind == 'extension' else '补充追问'))
            return decision
    def advance(self):
        s = self.session
        if s['phase'] == 'evaluate':
            turn = s['turns'][-1]
            if turn.get('response_status')=='unable':
                decision = FollowDecision(action='next',reason='unable')
            else:
                decision = self._decide(turn)
            s['decisions'].append({'main_index': s['main_index'], 'turn_id': turn['turn_id'], **decision.model_dump()})
            if decision.action == 'follow_up':
                s.update(phase='awaiting_answer', current_kind='follow_up', current_question=decision.question)
            else:
                s['phase'] = 'advance'
            self.store.save(s)
        if s['phase'] == 'advance':
            s['main_index'] += 1
            if s['main_index'] >= len(s['questions']): s['phase'] = 'summary'
            else: s.update(phase='awaiting_answer', current_kind='main', current_question=s['questions'][s['main_index']])
            self.store.save(s)
        if s['phase'] == 'summary':
            annotate_legacy(s)
            valid_turns = [t for t in s['turns'] if t.get('response_status') not in {'invalid_input','skipped'}]
            attempted = sorted({t['main_index'] for t in s['turns']})
            unassessed = [i for i in attempted if not any(t['main_index']==i for t in valid_turns)]
            required = [i for i in attempted if i not in unassessed]
            candidate = s.get('feedback_candidate') or {}
            retained = []
            for index in required:
                matches = [q for q in candidate.get('questions', []) if q.get('main_index') == index]
                if len(matches) != 1: continue
                try:
                    item = CoachItem.model_validate(matches[0])
                    subset = {**s, 'turns': [t for t in s['turns'] if t['main_index'] == index]}
                    validate_coaching(CoachFeedback(overall_assessment='', questions=[item], next_actions=[]), subset)
                    retained.append(item)
                except (ValueError, TypeError):
                    continue
            missing = [i for i in required if i not in {q.main_index for q in retained}]
            if missing:
                # Bind array size to this call, not just a prompt instruction.
                output_model = create_model('RequiredCoachFeedback', __base__=CoachFeedback,
                    questions=(list[CoachItem], Field(min_length=len(missing), max_length=len(missing))))
                feedback = self._call('mock_interview_feedback.md', {'jd_profile': s['jd_profile'], 'recruitment_process': s.get('recruitment_process'),
                    'candidate_evidence': s['evidence'], 'evidence_truncated': s['evidence_truncated'],
                    'transcript': [t for t in valid_turns if t['main_index'] in missing],
                    'main_questions': [{'main_index':i, 'question':s['questions'][i]} for i in missing],
                    'follow_up_decisions': [d for d in s['decisions'] if d['main_index'] in missing],
                    'required_main_indices': missing, 'required_question_count': len(missing),
                    'scope': 'Only coach the listed question groups; do not renumber indices.',
                    'ended_early': s.get('ended_early', False)}, output_model)
                # Keep all accepted earlier groups. Unexpected/duplicate returned groups
                # remain in the candidate and will fail coverage validation.
                feedback.questions.extend(retained)
                feedback.overall_assessment = '逐题教练反馈已汇总；本轮练习覆盖有限，请结合下方具体建议继续练习。'
                feedback.next_actions = [q.practice_follow_up for q in feedback.questions if q.practice_follow_up][:6]
            elif required:
                feedback = CoachFeedback(overall_assessment='逐题教练反馈已汇总；本轮练习覆盖有限。',
                    questions=retained, next_actions=[q.practice_follow_up for q in retained if q.practice_follow_up][:6])
            else:
                feedback = CoachFeedback(overall_assessment='本轮没有可评估的回答，可以重新练习。',questions=[],next_actions=['重新回答一道主问题。'])
            # Invalid input never enters the model prompt, and never gets a score.
            feedback.questions.extend(unevaluated(i, [t['turn_id'] for t in s['turns'] if t['main_index']==i]) for i in unassessed)
            feedback.questions.sort(key=lambda q:q.main_index)
            s['feedback_candidate'] = feedback.model_dump()
            self.store.save(s)
            validate_coaching(feedback, s)
            s.update(feedback=feedback.model_dump(), phase='completed', completed_at=utc_now(),
                     feedback_policy_version='1.7.2.2', feedback_revision_pending=False)
            self.store.save(s)
        return s

class OfflineInterviewExecutor:
    """Deterministic workflow test, not a real interview-quality evaluator."""
    def execute(self, skill, context, output_model):
        if output_model is FollowDecision:
            answer = context['candidate_answer']
            return FollowDecision(action='follow_up', reason='unclear', answer_quote=answer[:20],
                                  question='你刚才提到的处理思路，具体会用什么依据判断是否有效？')
        items=[]
        for question in context['main_questions']:
            index = question['main_index'] if isinstance(question, dict) else question
            turns=[t for t in context['transcript'] if t['main_index']==int(index)]
            unable=all(t.get('response_status')=='unable' for t in turns)
            items.append(CoachItem(main_index=int(index),level='unable' if unable else 'needs_more',
                turn_ids=[t['turn_id'] for t in turns],what_worked=[],gaps=['Mock占位：需要具体说明依据。'],optional_tips=[],
                supplement='' if unable else 'Mock占位：补充判断依据。',reasoning_steps=['先澄清目标，再说明步骤。'] if unable else [],
                reference_answer='以下是假设思路：先澄清目标和约束，再验证方案。' if unable else '',practice_follow_up='如何验证？',
                fact_status='clear',fact_notes=[],evidence_ids=[]))
        return CoachFeedback(overall_assessment='Mock模式：仅验证流程，不评估真实面试能力。',questions=items,next_actions=['用真实模型再练一轮。'])
