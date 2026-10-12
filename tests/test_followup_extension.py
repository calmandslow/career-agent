import tempfile
import unittest

from career_agent.live_interview import (
    FollowDecision, FollowDecisionError, InterviewEngine, OfflineInterviewExecutor,
    SessionStore, start_session,
)
from career_agent.models import ApplicationState
from career_agent.web_service import WebService


ANSWER = '失败时自动重试，并保存状态，避免重复执行已完成步骤。'


class SequenceExecutor(OfflineInterviewExecutor):
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.contexts = []

    def execute(self, skill, context, model):
        if model is FollowDecision:
            self.contexts.append(context)
            result = next(self.decisions)
            if isinstance(result, Exception):
                raise result
            return result
        return super().execute(skill, context, model)


def extension(reason='extension'):
    return FollowDecision(action='follow_up', reason=reason, answer_quality='complete',
        follow_up_kind='extension', answer_quote='自动重试',
        rationale='在已说明重试处理后，延伸考察请求成功但响应丢失时的重复副作用风险。',
        question='如果请求已经执行成功，但响应丢失，重试时如何避免重复产生副作用？')


def invalid():
    return FollowDecision(action='follow_up', reason='unclear',
                          answer_quote='我上线过产品', question='上线效果如何？')


class FollowupExtensionTests(unittest.TestCase):
    def state(self):
        return ApplicationState(interview_prep_profile={'questions':[
            {'question': '工具失败时如何处理？', 'priority': 'P0'},
            {'question': '如何验证方案？', 'priority': 'P1'}]})

    def engine(self, directory, executor):
        store = SessionStore(directory)
        session = start_session(self.state())
        store.save(session)
        engine = InterviewEngine(store, session, lambda: executor)
        engine.submit(ANSWER)
        return engine, session, store

    def test_complete_answer_can_extend_with_sufficient_reason_and_only_once(self):
        with tempfile.TemporaryDirectory() as d:
            executor = SequenceExecutor([extension('sufficient')])
            engine, s, store = self.engine(d, executor)
            engine.advance()
            self.assertEqual(s['current_kind'], 'follow_up')
            self.assertEqual(s['decisions'][0]['answer_quality'], 'complete')
            self.assertEqual(s['decisions'][0]['follow_up_kind'], 'extension')
            self.assertEqual(s['decision_attempts'][0]['accepted'], True)
            engine.submit('用幂等键识别重复请求。')
            engine.advance()
            self.assertEqual(s['main_index'], 1)
            self.assertEqual(s['current_kind'], 'main')
            self.assertEqual(len(executor.contexts), 1)
            self.assertEqual(len(store.load(s['session_id'])['turns']), 2)

    def test_complete_answer_can_also_move_on_without_forced_extension(self):
        with tempfile.TemporaryDirectory() as d:
            executor = SequenceExecutor([FollowDecision(action='next', reason='sufficient', answer_quality='complete')])
            engine, s, _ = self.engine(d, executor)
            engine.advance()
            self.assertEqual(s['main_index'], 1)
            self.assertEqual(len(s['turns']), 1)

    def test_extension_missing_purpose_is_repaired_with_specific_error(self):
        with tempfile.TemporaryDirectory() as d:
            bad = extension('sufficient').model_copy(update={'rationale': ''})
            executor = SequenceExecutor([bad, extension()])
            engine, s, _ = self.engine(d, executor)
            engine.advance()
            self.assertEqual(len(executor.contexts), 2)
            repair = executor.contexts[1]['repair']
            self.assertIn('延伸追问', repair['validation_issue'])
            self.assertEqual(repair['previous_decision']['answer_quality'], 'complete')
            self.assertEqual([a['accepted'] for a in s['decision_attempts']], [False, True])
            self.assertEqual(s['current_kind'], 'follow_up')

    def test_rejected_decision_persisted_and_repeated_resume_does_not_pay_again(self):
        with tempfile.TemporaryDirectory() as d:
            executor = SequenceExecutor([invalid(), invalid()])
            engine, s, store = self.engine(d, executor)
            with self.assertRaisesRegex(FollowDecisionError, '一次修正'):
                engine.advance()
            saved = store.load(s['session_id'])
            self.assertEqual(saved['phase'], 'evaluate')
            self.assertEqual(saved['turns'][0]['answer'], ANSWER)
            self.assertEqual(saved['decisions'], [])
            self.assertEqual(len(saved['decision_attempts']), 2)
            self.assertEqual(saved['decision_candidate']['repair_count'], 1)
            def forbidden():
                raise AssertionError('must not call API again')
            resumed = InterviewEngine(store, saved, forbidden)
            with self.assertRaises(FollowDecisionError):
                resumed.advance()
            resumed.move_on()
            resumed.advance()
            self.assertEqual(saved['main_index'], 1)
            self.assertEqual(saved['turns'][0]['response_status'], 'answered')
            self.assertEqual(saved['decisions'][0]['source'], 'user')

    def test_network_error_during_repair_resumes_repair_without_regeneration(self):
        with tempfile.TemporaryDirectory() as d:
            executor = SequenceExecutor([invalid(), RuntimeError('network')])
            engine, s, store = self.engine(d, executor)
            with self.assertRaisesRegex(RuntimeError, 'network'):
                engine.advance()
            saved = store.load(s['session_id'])
            replacement = SequenceExecutor([extension()])
            InterviewEngine(store, saved, lambda: replacement).advance()
            self.assertIn('repair', replacement.contexts[0])
            self.assertEqual(len(replacement.contexts), 1)
            self.assertEqual(len(saved['turns']), 1)

    def test_explicit_unable_does_not_trigger_extension_or_model(self):
        with tempfile.TemporaryDirectory() as d:
            store = SessionStore(d); s = start_session(self.state())
            def forbidden():
                raise AssertionError('unexpected model')
            engine = InterviewEngine(store, s, forbidden)
            engine.submit('我不会。')
            engine.advance()
            self.assertEqual(s['decisions'][0]['reason'], 'unable')
            self.assertEqual(s['main_index'], 1)

    def test_old_pending_session_can_continue_without_new_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            executor = SequenceExecutor([extension()])
            engine, s, store = self.engine(d, executor)
            s.pop('decision_attempts', None); s.pop('decision_candidate', None)
            store.save(s)
            resumed = store.load(s['session_id'])
            InterviewEngine(store, resumed, lambda: executor).advance()
            self.assertEqual(resumed['current_kind'], 'follow_up')
            self.assertEqual(len(resumed['turns']), 1)

    def test_web_move_on_preserves_answer_and_retry_cannot_advance_newer_turn(self):
        with tempfile.TemporaryDirectory() as d:
            svc = WebService(d, 'mock')
            try:
                state = self.state(); svc.repo.save(state)
                s = svc.new_session(state.application_id, 2)
                engine = InterviewEngine(svc.sessions, s, lambda: OfflineInterviewExecutor())
                engine.submit(ANSWER)
                task = svc.start_task('interview', state.application_id, {'session_id': s['session_id'], 'operation': 'move_on'})
                result = svc.tasks.wait(task['task_id'])
                self.assertEqual(result['status'], 'completed')
                saved = svc.sessions.load(s['session_id'])
                self.assertEqual(saved['main_index'], 1)
                self.assertEqual(saved['llm_calls'], [])
                self.assertEqual(saved['turns'][0]['answer'], ANSWER)
                engine = InterviewEngine(svc.sessions, saved, lambda: OfflineInterviewExecutor())
                engine.submit('新问题的回答')
                class Silent:
                    def emit(self, message): pass
                svc.execute(task, Silent())
                saved = svc.sessions.load(s['session_id'])
                self.assertEqual(saved['phase'], 'evaluate')
                self.assertEqual(len(saved['decisions']), 1)
                self.assertEqual(len(saved['turns']), 2)
            finally:
                svc.tasks.close()

    def test_last_question_move_on_retries_feedback_without_skipping_answer(self):
        class FailOnce(OfflineInterviewExecutor):
            def __init__(self): self.calls = 0
            def execute(self, skill, context, model):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError('temporary feedback failure')
                return super().execute(skill, context, model)
        with tempfile.TemporaryDirectory() as d:
            executor = FailOnce()
            svc = WebService(d, 'qwen', executor_factory=lambda provider, progress: executor)
            try:
                state = self.state(); svc.repo.save(state)
                s = svc.new_session(state.application_id, 1)
                InterviewEngine(svc.sessions, s, lambda: executor).submit(ANSWER)
                task = svc.start_task('interview', state.application_id, {'session_id': s['session_id'], 'operation': 'move_on'})
                self.assertEqual(svc.tasks.wait(task['task_id'])['status'], 'failed')
                self.assertEqual(svc.sessions.load(s['session_id'])['phase'], 'summary')
                retry = svc.tasks.retry(task['task_id'])
                self.assertEqual(svc.tasks.wait(retry['task_id'])['status'], 'completed')
                saved = svc.sessions.load(s['session_id'])
                self.assertEqual(saved['phase'], 'completed')
                self.assertEqual(len(saved['turns']), 1)
                self.assertEqual(len(saved['decisions']), 1)
                self.assertEqual(saved['turns'][0]['response_status'], 'answered')
            finally:
                svc.tasks.close()
