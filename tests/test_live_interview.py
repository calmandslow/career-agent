import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from career_agent.live_interview import (SessionStore, start_session, InterviewEngine, OfflineInterviewExecutor,
    FollowDecision, SessionFeedback)
from career_agent.models import ApplicationState

class LiveInterviewTests(unittest.TestCase):
    def state(self):
        return ApplicationState(interview_prep_profile={'questions':[
            {'question':'如何衡量推荐效果？','priority':'P1'},
            {'question':'请介绍你的Agent项目。','priority':'P0'},
            {'question':'假设成本过高，你会如何处理？','priority':'P0'}]})
    def test_question_selection_is_unique_and_priority_ordered(self):
        state=self.state()
        state.interview_prep_profile['questions'].append({'question':'请介绍你的Agent项目。','priority':'P0'})
        s=start_session(state)
        self.assertEqual(s['questions'],['请介绍你的Agent项目。','假设成本过高，你会如何处理？','如何衡量推荐效果？'])
        self.assertNotIn('answer_prep_profile',s)
    def test_blank_bank_and_invalid_count_fail_before_api(self):
        with self.assertRaises(ValueError): start_session(ApplicationState())
        with self.assertRaises(ValueError): start_session(self.state(),count=0)
    def test_full_round_max_one_followup_and_no_source_state_mutation(self):
        state=self.state();before=copy.deepcopy(state.to_dict())
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(state,provider='mock');store.save(s)
            engine=InterviewEngine(store,s,lambda:OfflineInterviewExecutor())
            for i in range(3):
                self.assertEqual(s['current_kind'],'main')
                engine.submit('我会先澄清目标，再选择评估方法。');engine.advance()
                self.assertEqual(s['current_kind'],'follow_up')
                self.assertIsNone(s['feedback'])
                engine.submit('我会明确指标，并检查结果。');engine.advance()
            self.assertEqual(s['phase'],'completed')
            self.assertEqual(len(s['turns']),6)
            self.assertEqual(len(s['decisions']),3)
            self.assertEqual(store.load(s['session_id'])['feedback'],s['feedback'])
        self.assertEqual(state.to_dict(),before)
    def test_network_failure_retains_answer_and_resume_does_not_resubmit(self):
        class Failing:
            def execute(self,*args): raise RuntimeError('network')
            def drain_call_records(self): return [{'status':'failed'}]
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            engine=InterviewEngine(store,s,lambda:Failing());engine.submit('先澄清目标。')
            with self.assertRaises(RuntimeError):engine.advance()
            restored=store.load(s['session_id'])
            self.assertEqual(restored['phase'],'evaluate');self.assertEqual(len(restored['turns']),1)
            self.assertEqual(restored['llm_calls'][0]['status'],'failed')
            resumed=InterviewEngine(store,restored,lambda:OfflineInterviewExecutor());resumed.advance()
            self.assertEqual(len(restored['turns']),1)
            self.assertEqual(restored['current_kind'],'follow_up')
    def test_followup_answer_resume_advances_without_another_decision(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            engine=InterviewEngine(store,s,lambda:OfflineInterviewExecutor())
            engine.submit('先澄清目标。');engine.advance();engine.submit('再核实约束。')
            restored=store.load(s['session_id'])
            def forbidden(): raise AssertionError('unexpected API')
            InterviewEngine(store,restored,forbidden).advance()
            self.assertEqual(restored['main_index'],1)
            self.assertEqual(restored['current_kind'],'main')
    def test_sufficient_answer_moves_to_next_without_followup(self):
        class Next:
            def execute(self,*args): return FollowDecision(action='next',reason='sufficient')
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            engine=InterviewEngine(store,s,lambda:Next());engine.submit('完整回答');engine.advance()
            self.assertEqual(s['main_index'],1);self.assertEqual(len(s['turns']),1)
    def test_unrelated_followup_quote_rejected_and_progress_retained(self):
        class Invalid:
            def execute(self,*args): return FollowDecision(action='follow_up',reason='unclear',answer_quote='我上线过产品',question='上线效果如何？')
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            engine=InterviewEngine(store,s,lambda:Invalid());engine.submit('我只做过设计。')
            with self.assertRaises(ValueError):engine.advance()
            self.assertEqual(store.load(s['session_id'])['phase'],'evaluate')
            self.assertEqual(s['decisions'],[])
    def test_skip_does_not_make_followup_call(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            def forbidden(): raise AssertionError('unexpected API')
            engine=InterviewEngine(store,s,forbidden);engine.skip();engine.advance()
            self.assertEqual(s['main_index'],1);self.assertTrue(s['turns'][0]['skipped'])
    def test_early_finish_summary_can_retry(self):
        class Failing:
            def execute(self,*args):raise RuntimeError('network')
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            engine=InterviewEngine(store,s,lambda:Failing());engine.submit('实际回答');engine.finish_early()
            with self.assertRaises(RuntimeError):engine.advance()
            restored=store.load(s['session_id'])
            InterviewEngine(store,restored,lambda:OfflineInterviewExecutor()).advance()
            self.assertEqual(restored['phase'],'completed');self.assertTrue(restored['ended_early'])
            self.assertEqual(len(restored['turns']),1)
    def test_feedback_invalid_turn_is_not_accepted(self):
        class Invalid(OfflineInterviewExecutor):
            def execute(self,skill,context,model):
                feedback=super().execute(skill,context,model);feedback.questions[0].turn_ids=[999];return feedback
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            engine=InterviewEngine(store,s,lambda:Invalid());engine.submit('实际回答');engine.finish_early()
            with self.assertRaises(ValueError):engine.advance()
            self.assertIsNone(s['feedback']);self.assertEqual(s['phase'],'summary')
            self.assertIn('feedback_candidate',store.load(s['session_id']))
    def test_resume_completed_does_not_call_model(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state(),count=1);store.save(s)
            engine=InterviewEngine(store,s,lambda:OfflineInterviewExecutor());engine.skip();engine.advance()
            def forbidden(): raise AssertionError('unexpected API')
            InterviewEngine(store,store.load(s['session_id']),forbidden).advance()
    def test_session_path_validation_and_latest_by_provider(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);state=self.state()
            mock=start_session(state,provider='mock');store.save(mock)
            real=start_session(state,provider='qwen');store.save(real)
            self.assertEqual(store.latest(state.application_id,'mock')['session_id'],mock['session_id'])
            with self.assertRaises(ValueError):store.load('../escape')
    def test_no_answer_submission_when_evaluation_is_pending(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=start_session(self.state());store.save(s)
            engine=InterviewEngine(store,s,lambda:OfflineInterviewExecutor())
            with self.assertRaises(ValueError):engine.submit(' ')
            engine.submit('回答')
            with self.assertRaises(ValueError):engine.submit('重复回答')
    def test_evidence_budget(self):
        state=self.state();state.material_evidence={'portfolio':[{'evidence_id':'LONG','text':'x'*100000}]}
        s=start_session(state)
        self.assertTrue(s['evidence_truncated']);self.assertLess(len(str(s['evidence'])),12000)
    def test_cli_mock_pause_and_resume_exports_feedback(self):
        import qwen_mock_interview as cli
        from career_agent.repository import JsonStateRepository
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);state=self.state();JsonStateRepository(root/'data'/'applications').save(state)
            argv=['mock',state.application_id,'--provider','mock','--questions','1']
            with patch.object(cli,'PROJECT_ROOT',root),patch('sys.argv',argv),patch('builtins.input',side_effect=['回答','/pause']),patch('builtins.print'):
                cli.main()
            session=SessionStore(root/'data'/'mock_interviews').latest(state.application_id,'mock')
            self.assertEqual(session['current_kind'],'follow_up')
            with patch.object(cli,'PROJECT_ROOT',root),patch('sys.argv',argv),patch('builtins.input',return_value='追问回答'),patch('builtins.print'):
                cli.main()
            self.assertEqual(len(list((root/'outputs').rglob('feedback.md'))),1)
            self.assertEqual(len(list((root/'outputs').rglob('cost_report.md'))),1)
            self.assertEqual(JsonStateRepository(root/'data'/'applications').load(state.application_id).to_dict(),state.to_dict())
