"""CLI wrapper for the persisted interview engine. /pause, /skip, /finish."""
import argparse
import json
from pathlib import Path
from career_agent.interview_coach import command_input
from career_agent.live_interview import SessionStore, start_session, InterviewEngine, OfflineInterviewExecutor
from career_agent.repository import JsonStateRepository
from career_agent.executor_factory import create_executor
from career_agent.telemetry import ProgressReporter
from career_agent.reporting import write_run_reports

PROJECT_ROOT = Path(__file__).resolve().parent

def export_session(session, state):
    directory = PROJECT_ROOT / 'outputs' / session['application_id'] / 'mock_interviews' / session['session_id']
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'session.json').write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding='utf-8')
    transcript = ['# 模拟面试记录', f"状态：{session['phase']}", '']
    for t in session['turns']:
        transcript.extend([f"## {t['turn_id']}. {'追问' if t['kind']=='follow_up' else '主问题'}", t['question'], '', t['answer'], ''])
    (directory / 'transcript.md').write_text('\n'.join(transcript), encoding='utf-8')
    feedback = session.get('feedback')
    if feedback:
        (directory / 'feedback.json').write_text(json.dumps(feedback, ensure_ascii=False, indent=2), encoding='utf-8')
        lines = ['# 模拟面试教练反馈', feedback['overall_assessment'], '']
        if session.get('feedback_revision_pending'):
            lines.extend(['**新反馈待生成，下方为旧版反馈，请勿视为本次更新结果。**',''])
        if 'questions' in feedback:
            labels={'complete':'回答较完整','needs_more':'需要补充','unable':'暂时不会回答','not_evaluated':'本题未评估'}
            for q in feedback['questions']:
                lines.extend([f"## 第 {q['main_index']+1} 题：{labels[q['level']]}", session['questions'][q['main_index']], ''])
                for field,title in [('what_worked','值得保留'),('gaps','必要补充'),('optional_tips','可选建议'),('fact_notes','事实确认或纠正'),('reasoning_steps','完整处理思路')]:
                    if q[field]: lines.extend([f'### {title}', '', *[f'- {x}' for x in q[field]], ''])
                for field,title in [('supplement','补充段落'),('reference_answer','参考回答（请核实个人事实）'),('practice_follow_up','建议重练的追问')]:
                    if q[field]: lines.extend([f'### {title}', '', q[field], ''])
            lines.extend(['## 下一步', '', *[f'- {x}' for x in feedback['next_actions']]])
        else:
            lines.extend(['旧版反馈已保留在会话的 feedback_history 中；使用 --feedback-only 生成新版。'])
        if session.get('feedback_history'):
            (directory / 'feedback_history.json').write_text(json.dumps(session['feedback_history'], ensure_ascii=False, indent=2), encoding='utf-8')
        (directory / 'feedback.md').write_text('\n'.join(lines), encoding='utf-8')
    # Use a reporting copy; do not contaminate source application history.
    from career_agent.models import ApplicationState
    report_state = ApplicationState.from_dict(state.to_dict())
    report_state.llm_calls = session['llm_calls']
    report_state.execution_history = []
    report_state.current_stage = 'mock_interview_' + session['phase']
    write_run_reports(directory, report_state, {'status':session['phase'], 'run_id':session['session_id']}, run_id=session['session_id'])
    return directory

def main():
    parser = argparse.ArgumentParser(description='Text mock interview; default 3 primary questions, max 1 follow-up each')
    parser.add_argument('application_id')
    parser.add_argument('--provider', choices=['qwen','mock'], default='qwen')
    parser.add_argument('--questions', type=int, default=3, help='New-session question count')
    parser.add_argument('--new', action='store_true', help='Start a new session; keep old records')
    parser.add_argument('--feedback-only', action='store_true', help='Regenerate only final coaching for a saved session; uses API')
    parser.add_argument('--session', help='Resume specific session ID')
    parser.add_argument('--move-on', action='store_true', help='Keep a saved pending main answer and move to the next question')
    args = parser.parse_args()
    if args.feedback_only and (args.new or not args.session): parser.error('--feedback-only requires --session and cannot use --new')
    if args.move_on and (args.new or args.feedback_only or not args.session): parser.error('--move-on requires --session and cannot use --new or --feedback-only')
    if args.new and args.session: parser.error('--new and --session cannot be combined')
    state = JsonStateRepository(PROJECT_ROOT/'data'/'applications').load(args.application_id)
    store = SessionStore(PROJECT_ROOT/'data'/'mock_interviews')
    session = store.load(args.session) if args.session else (None if args.new else store.latest(args.application_id, args.provider))
    if session is None:
        session = start_session(state, count=args.questions, provider=args.provider)
        store.save(session)
    if session['application_id'] != args.application_id or session['provider'] != args.provider:
        raise ValueError('Session application/provider does not match')
    factory = lambda: OfflineInterviewExecutor() if args.provider == 'mock' else create_executor('qwen', progress=ProgressReporter(enabled=True))
    engine = InterviewEngine(store, session, factory)
    print(f"Session: {session['session_id']} · {args.provider} · {len(session['questions'])} main question(s)")
    print('每次输入一行完整回答；/pause 保存退出，/skip 跳过当前题，/finish 提前结束并生成反馈。')
    try:
        if args.feedback_only: engine.redo_feedback()
        if args.move_on: engine.move_on()
        while True:
            engine.advance()
            if session['phase'] == 'completed':
                print('本轮已完成；用 --new 开始新一轮。')
                break
            label = '追问' if session['current_kind']=='follow_up' else '主问题'
            print(f"\n[{session['main_index']+1}/{len(session['questions'])}] {label}：{session['current_question']}")
            answer = input('你的回答> ').strip()
            command = command_input(answer)
            if command == 'invalid_command':
                print('疑似命令输入错误。请使用 /pause、/skip 或 /finish（正斜杠）；此输入未记为答案。'); continue
            if command in {'/pause','/quit'}:
                print('已保存；再次运行相同命令继续。'); break
            if command == '/finish':
                if not session['turns']:
                    print('请先回答至少一道问题。'); continue
                engine.finish_early(); continue
            if command == '/skip':
                engine.skip(); continue
            if not answer: continue
            engine.submit(answer)
    except (KeyboardInterrupt, EOFError):
        print('\n已保存当前进度；再次运行相同命令继续。')
    except Exception:
        print('本轮中断，已保留你的回答；再次运行相同命令继续待处理步骤。')
        raise
    finally:
        directory = export_session(session, state)
        print(f'Records and cost report: {directory}')
    if session.get('feedback'):
        print(session['feedback']['overall_assessment'])
        print(f"完整反馈：{directory / 'feedback.md'}")

if __name__ == '__main__': main()
