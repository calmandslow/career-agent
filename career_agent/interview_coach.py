"""Question-level coaching contracts; no hiring verdicts or numeric scores."""
import re
from typing import Literal
from pydantic import BaseModel, Field

class CoachItem(BaseModel):
    main_index: int = Field(ge=0)
    level: Literal['complete', 'needs_more', 'unable', 'not_evaluated']
    turn_ids: list[int]
    what_worked: list[str]
    gaps: list[str]
    optional_tips: list[str]
    supplement: str
    reasoning_steps: list[str]
    reference_answer: str
    practice_follow_up: str
    fact_status: Literal['clear', 'confirm', 'correct']
    fact_notes: list[str]
    evidence_ids: list[str]

class CoachFeedback(BaseModel):
    overall_assessment: str
    questions: list[CoachItem]
    next_actions: list[str]

COMMANDS = {'/pause','/quit','/skip','/finish'}

def command_input(text):
    """Known commands; a backslash typo is not an answer nor automatic execution."""
    clean = text.strip().lower()
    if clean in COMMANDS: return clean
    if re.fullmatch(r'[/\\\\／][a-zA-Z]+', clean): return 'invalid_command'
    return None

def explicitly_unable(text):
    clean = re.sub(r'[\s，。！？,.!?]', '', text)
    return clean in {'我不知道','不知道','我不会','不会','我不是很清楚','不清楚','我不清楚','我暂时无法回答这道题','暂时不会回答'}

def annotate_legacy(session):
    changes = []
    for turn in session['turns']:
        if turn.get('response_status'): continue
        if command_input(turn['answer']): status='invalid_input'
        elif turn.get('skipped'): status='skipped'
        elif explicitly_unable(turn['answer']): status='unable'
        else: status='answered'
        turn['response_status']=status
        changes.append({'turn_id':turn['turn_id'],'status':status})
    if changes: session.setdefault('annotations',[]).append({'reason':'1.7.2.1 response classification','changes':changes})
    # Preserve previous decisions; add interpretation, never reinterpret next as competence.
    for d in session['decisions']:
        main = next((t for t in session['turns'] if t['main_index']==d['main_index'] and t['kind']=='main'),None)
        if main and main.get('response_status') in {'unable','skipped','invalid_input'}:
            d['effective_reason']=main['response_status']

def unevaluated(index, ids):
    return CoachItem(main_index=index,level='not_evaluated',turn_ids=ids,what_worked=[],gaps=[],optional_tips=[],
        supplement='',reasoning_steps=[],reference_answer='',practice_follow_up='',fact_status='clear',fact_notes=[],evidence_ids=[])

def validate_coaching(feedback, session):
    text = feedback.model_dump_json()
    if any(x in text for x in ('无法胜任','不能胜任','完全不懂','完全不了解','不适合该岗位')):
        raise ValueError('Coaching must give suggestions, not categorical ability or hiring verdicts')
    groups = {i:[t for t in session['turns'] if t['main_index']==i] for i in range(len(session['questions']))}
    expected = {i for i, turns in groups.items() if turns}
    if len(feedback.questions)!=len(expected) or {q.main_index for q in feedback.questions}!=expected:
        raise ValueError('Coaching must cover each attempted primary question exactly once')
    evidence_ids = {item.get('evidence_id') for items in session.get('evidence',{}).values() if isinstance(items,list) for item in items if isinstance(item,dict)}
    for q in feedback.questions:
        turns = groups[q.main_index]; allowed={t['turn_id'] for t in turns}
        valid=[t for t in turns if t.get('response_status') not in {'invalid_input','skipped'}]
        if valid: allowed={t['turn_id'] for t in valid}
        if not q.turn_ids or set(q.turn_ids)-allowed: raise ValueError('Coaching cites wrong question turns')
        if set(q.evidence_ids)-evidence_ids: raise ValueError('Coaching cites unknown evidence')
        if not valid:
            if q.level!='not_evaluated' or q.reference_answer or q.supplement or q.gaps or q.fact_notes:
                raise ValueError('Invalid/skipped answers cannot be assessed')
            continue
        if q.level=='not_evaluated':
            raise ValueError('Valid responses require coaching, not an unassessed placeholder')
        if all(t.get('response_status')=='unable' for t in valid) and q.level!='unable':
            raise ValueError('Explicit inability needs full coaching, not a complete verdict')
        if q.level=='complete' and (q.gaps or q.supplement or q.reference_answer):
            raise ValueError('Complete answers should be preserved, not rewritten')
        if q.level=='needs_more' and (not q.gaps or not q.supplement or q.reference_answer):
            raise ValueError('Needs-more requires concrete gaps and a supplement, not a full rewrite')
        if q.level=='unable' and (not q.reasoning_steps or not q.reference_answer):
            raise ValueError('Unable answers need reasoning steps and a truthful reference answer')
        if q.fact_status!='clear' and not q.fact_notes: raise ValueError('Fact concern requires concrete explanation')
