import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from career_agent.interview_coach import (CoachFeedback, CoachItem, command_input, annotate_legacy, validate_coaching)
from career_agent.live_interview import SessionStore, InterviewEngine, start_session, OfflineInterviewExecutor
from career_agent.models import ApplicationState

class CoachTests(unittest.TestCase):
    def session(self):
        state=ApplicationState(interview_prep_profile={'questions':[{'question':'假设成本过高，你会如何协调？'}]})
        return start_session(state,count=1,provider='mock')
    def item(self, **updates):
        values=dict(main_index=0,level='needs_more',turn_ids=[1],what_worked=['已有降级思路'],gaps=['没有验收条件'],
            optional_tips=[],supplement='我会先和业务确认最低验收条件。',reasoning_steps=[],reference_answer='',
            practice_follow_up='如何定义验收？',fact_status='clear',fact_notes=[],evidence_ids=[])
        return CoachItem(**{**values,**updates})
    def feedback(self,item):
        return CoachFeedback(overall_assessment='这次回答有思路，可以补充验收条件。',questions=[item],next_actions=['练习说明验收条件。'])
    def answered(self,status='answered'):
        s=self.session();s['turns']=[dict(turn_id=1,main_index=0,kind='main',question=s['questions'][0],answer='先上线降级版本',response_status=status)]
        return s
    def test_backslash_command_and_unknown_slash_are_not_answers(self):
        for value in ['\\finish','/finsih','／finish']:self.assertEqual(command_input(value),'invalid_command')
        self.assertEqual(command_input('/finish'),'/finish')
        self.assertIsNone(command_input('A/B测试'))
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=self.session();store.save(s)
            engine=InterviewEngine(store,s,lambda:OfflineInterviewExecutor())
            with self.assertRaises(ValueError):engine.submit('\\finish')
            self.assertEqual(s['turns'],[])
    def test_unknown_answer_moves_on_without_sufficient_label_or_api(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=self.session();s['questions'].append('另一题');store.save(s)
            def forbidden():raise AssertionError('Should not call API for explicit inability')
            engine=InterviewEngine(store,s,forbidden);engine.submit('我不是很清楚');engine.advance()
            self.assertEqual(s['decisions'][0]['reason'],'unable')
            self.assertEqual(s['turns'][0]['response_status'],'unable')
            self.assertEqual(s['main_index'],1)
    def test_legacy_annotations_preserve_raw_transcript_and_decisions(self):
        s=self.answered();s['turns'][0].pop('response_status');s['turns'][0]['answer']='\\finish'
        s['decisions']=[{'main_index':0,'action':'next','reason':'sufficient'}]
        original=copy.deepcopy(s['turns'][0]);annotate_legacy(s)
        self.assertEqual(s['turns'][0]['answer'],original['answer'])
        self.assertEqual(s['turns'][0]['response_status'],'invalid_input')
        self.assertEqual(s['decisions'][0]['reason'],'sufficient')
        self.assertEqual(s['decisions'][0]['effective_reason'],'invalid_input')
    def test_complete_preserves_answer_and_rejects_full_rewrite(self):
        s=self.answered()
        item=self.item(level='complete',gaps=[],supplement='')
        validate_coaching(self.feedback(item),s)
        item.reference_answer='重写整段'
        with self.assertRaises(ValueError):validate_coaching(self.feedback(item),s)
    def test_needs_more_requires_specific_gaps_and_supplement(self):
        s=self.answered()
        validate_coaching(self.feedback(self.item()),s)
        with self.assertRaises(ValueError):validate_coaching(self.feedback(self.item(gaps=[])),s)
        with self.assertRaises(ValueError):validate_coaching(self.feedback(self.item(reference_answer='全新答案')),s)
    def test_explicit_unable_requires_complete_reasoning(self):
        s=self.answered('unable')
        with self.assertRaises(ValueError):validate_coaching(self.feedback(self.item()),s)
        item=self.item(level='unable',supplement='',reasoning_steps=['确认目标','比较成本','定义验收'],reference_answer='以下是假设思路：我会先确认目标和约束。')
        validate_coaching(self.feedback(item),s)
    def test_fact_concern_needs_explanation(self):
        with self.assertRaises(ValueError):validate_coaching(self.feedback(self.item(fact_status='confirm')),self.answered())
        validate_coaching(self.feedback(self.item(fact_status='confirm',fact_notes=['材料未确认上线情况，请用户补充。'])),self.answered())
    def test_unknown_evidence_rejected(self):
        with self.assertRaises(ValueError):validate_coaching(self.feedback(self.item(evidence_ids=['FAKE'])),self.answered())
    def test_valid_answer_cannot_be_left_unassessed(self):
        with self.assertRaises(ValueError):
            validate_coaching(self.feedback(self.item(level='not_evaluated')),self.answered())
    def test_nonzero_indices_and_required_count_are_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.answered();s['turns'][0].update(answer='\\finish',response_status='invalid_input')
            s['questions'].extend(['第二题','第三题'])
            s['turns'].extend([dict(turn_id=i+1,main_index=i,kind='main',question=s['questions'][i],
                answer='我不是很清楚',response_status='unable') for i in [1,2]])
            s['phase']='summary';store=SessionStore(d);store.save(s)
            class Capture(OfflineInterviewExecutor):
                def execute(inner,skill,context,model):
                    self.assertEqual(context['required_main_indices'],[1,2])
                    self.assertEqual([q['main_index'] for q in context['main_questions']],[1,2])
                    with self.assertRaises(ValueError):
                        model.model_validate(dict(overall_assessment='只有总评',questions=[],next_actions=[]))
                    return super().execute(skill,context,model)
            InterviewEngine(store,s,lambda:Capture()).advance()
            self.assertEqual([q['main_index'] for q in s['feedback']['questions']],[0,1,2])
    def test_retry_generates_only_missing_group_and_keeps_accepted_feedback(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.answered();s['questions'].append('第二题')
            s['turns'].append(dict(turn_id=2,main_index=1,kind='main',question='第二题',answer='先确认目标',response_status='answered'))
            s['phase']='summary';s['feedback_candidate']=self.feedback(self.item()).model_dump()
            original=copy.deepcopy(s['feedback_candidate']['questions'][0])
            store=SessionStore(d);store.save(s)
            class Capture(OfflineInterviewExecutor):
                def execute(inner,skill,context,model):
                    self.assertEqual(context['required_main_indices'],[1])
                    self.assertEqual([t['turn_id'] for t in context['transcript']],[2])
                    return super().execute(skill,context,model)
            InterviewEngine(store,s,lambda:Capture()).advance()
            self.assertEqual(s['feedback']['questions'][0],original)
    def test_retry_complete_candidate_finishes_without_api(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.answered();s['phase']='summary';s['feedback_candidate']=self.feedback(self.item()).model_dump()
            store=SessionStore(d);store.save(s)
            def forbidden():raise AssertionError('Complete valid candidate needs no API')
            InterviewEngine(store,s,forbidden).advance()
            self.assertEqual(s['phase'],'completed')
    def test_duplicate_returned_groups_remain_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.answered();s['questions'].append('第二题')
            s['turns'].append(dict(turn_id=2,main_index=1,kind='main',question='第二题',answer='回答',response_status='answered'))
            s['phase']='summary';store=SessionStore(d);store.save(s)
            class Duplicate:
                def execute(inner,*args):
                    f=self.feedback(self.item());f.questions.append(self.item());return f
            with self.assertRaisesRegex(ValueError,'exactly once'):
                InterviewEngine(store,s,lambda:Duplicate()).advance()
            self.assertIsNone(s['feedback'])
    def test_new_revision_does_not_reuse_previous_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.answered();s['phase']='completed';s['feedback_candidate']=self.feedback(self.item()).model_dump()
            store=SessionStore(d);store.save(s)
            InterviewEngine(store,s,lambda:OfflineInterviewExecutor()).redo_feedback()
            self.assertNotIn('feedback_candidate',s)
    def test_hiring_verdict_rejected_without_extra_llm_judge(self):
        f=self.feedback(self.item());f.overall_assessment='你无法胜任这个岗位。'
        with self.assertRaisesRegex(ValueError,'categorical'):validate_coaching(f,self.answered())
    def test_duplicate_or_missing_question_feedback_rejected(self):
        f=self.feedback(self.item());f.questions.append(self.item())
        with self.assertRaises(ValueError):validate_coaching(f,self.answered())
    def test_only_invalid_inputs_produces_unassessed_without_api(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=self.answered();s['turns'][0]['answer']='\\finish';s['turns'][0].pop('response_status');s['phase']='summary';store.save(s)
            def forbidden():raise AssertionError('Invalid transcript must not enter API')
            InterviewEngine(store,s,forbidden).advance()
            self.assertEqual(s['feedback']['questions'][0]['level'],'not_evaluated')
            self.assertEqual(s['feedback']['questions'][0]['reference_answer'],'')
    def test_redo_feedback_failure_preserves_old_report_and_retry(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=self.answered();s['phase']='completed';s['feedback']={'overall_assessment':'旧版反馈'};store.save(s)
            class Fail:
                def execute(self,*args):raise RuntimeError('network')
            engine=InterviewEngine(store,s,lambda:Fail());engine.redo_feedback()
            with self.assertRaises(RuntimeError):engine.advance()
            self.assertEqual(s['feedback']['overall_assessment'],'旧版反馈')
            self.assertEqual(len(s['feedback_history']),1)
            engine.redo_feedback();self.assertEqual(len(s['feedback_history']),1)
            InterviewEngine(store,s,lambda:OfflineInterviewExecutor()).advance()
            self.assertFalse(s['feedback_revision_pending']);self.assertEqual(len(s['turns']),1)
            self.assertEqual(s['feedback']['questions'][0]['level'],'needs_more')
    def test_invalid_inputs_are_removed_from_real_feedback_context(self):
        with tempfile.TemporaryDirectory() as d:
            store=SessionStore(d);s=self.answered();s['questions'].append('第二题');s['turns'][0].update(answer='\\finish',response_status='invalid_input')
            s['turns'].append(dict(turn_id=2,main_index=1,kind='main',question='第二题',answer='我不是很清楚',response_status='unable'))
            s['phase']='summary';store.save(s)
            class Capture(OfflineInterviewExecutor):
                def execute(inner,skill,context,model):
                    self.assertEqual([t['turn_id'] for t in context['transcript']],[2])
                    return super().execute(skill,context,model)
            InterviewEngine(store,s,lambda:Capture()).advance()
            self.assertEqual([q['level'] for q in s['feedback']['questions']],['not_evaluated','unable'])
    def test_cli_typo_reprompts_without_creating_transcript_turn(self):
        import qwen_mock_interview as cli
        from career_agent.repository import JsonStateRepository
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);state=ApplicationState(interview_prep_profile={'questions':[{'question':'第一题'}]});JsonStateRepository(root/'data'/'applications').save(state)
            with patch.object(cli,'PROJECT_ROOT',root),patch('sys.argv',['mock',state.application_id,'--provider','mock']),patch('builtins.input',side_effect=['\\finish','/pause']),patch('builtins.print'):
                cli.main()
            s=SessionStore(root/'data'/'mock_interviews').latest(state.application_id,'mock')
            self.assertEqual(s['turns'],[])
