import tempfile
import unittest

from career_agent.live_interview import (
    FollowDecision, FollowDecisionError, InterviewEngine, SessionStore,
    start_session, validate_follow_decision,
)
from career_agent.models import ApplicationState


class FollowupWhitespaceTests(unittest.TestCase):
    def decision(self, quote):
        return FollowDecision(action='follow_up', reason='unclear',
            follow_up_kind='supplement', answer_quote=quote, question='具体怎么处理失败？')

    def test_whitespace_normalization_preserves_exact_source_quote(self):
        source = '理解文本交给LLM   外部工具\n\t负责搜索'
        decision = self.decision('理解文本交给LLM 外部工具 负责搜索')
        result = validate_follow_decision(decision, {'answer': source})
        self.assertEqual(result.answer_quote, source)
        self.assertEqual(decision.answer_quote, '理解文本交给LLM 外部工具 负责搜索')

    def test_exact_quote_remains_valid(self):
        result = validate_follow_decision(self.decision('外部工具负责搜索'), {'answer': '我说外部工具负责搜索。'})
        self.assertEqual(result.answer_quote, '外部工具负责搜索')

    def test_meaning_numbers_punctuation_and_added_spaces_are_not_normalized(self):
        for actual, quote in [('我没有上线产品', '我已经上线产品'),
                              ('失败率 10%', '失败率 20%'),
                              ('重试，保留状态', '重试。保留状态'),
                              ('模型验证文本', '模型 验证文本')]:
            with self.subTest(actual=actual, quote=quote):
                with self.assertRaises(FollowDecisionError):
                    validate_follow_decision(self.decision(quote), {'answer': actual})

    def test_saved_rejected_decision_can_pass_without_another_model_call(self):
        state = ApplicationState(interview_prep_profile={'questions':[{'question':'如何处理失败？','priority':'P0'}]})
        with tempfile.TemporaryDirectory() as d:
            store = SessionStore(d); s = start_session(state)
            def forbidden(): raise AssertionError('cached decision must not call model')
            engine = InterviewEngine(store, s, forbidden)
            source = '理解文本交给LLM   外部工具如同手脚'
            engine.submit(source)
            bad = self.decision('理解文本交给LLM 外部工具如同手脚').model_dump()
            s['decision_candidate'] = {'turn_id':1,'output':bad,'repair_count':1,'recorded':True}
            s['decision_attempts'] = [{'turn_id':1,'output':bad,'accepted':False,'validation_issue':'旧版空格校验失败'}]
            s['llm_calls'] = [{'status':'completed','total_tokens':2173}]
            store.save(s)
            restored = store.load(s['session_id'])
            InterviewEngine(store, restored, forbidden).advance()
            self.assertEqual(restored['current_kind'], 'follow_up')
            self.assertEqual(restored['decisions'][0]['answer_quote'], source)
            self.assertEqual(len(restored['turns']), 1)
            self.assertEqual(len(restored['llm_calls']), 1)
            self.assertEqual([a['accepted'] for a in restored['decision_attempts']], [False, True])


if __name__ == '__main__': unittest.main()
