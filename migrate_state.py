"""Copy validated application/session records from an older local version; never overwrite."""
import argparse
import json
import shutil
from pathlib import Path
from career_agent.models import ApplicationState
from career_agent.web_service import safe_id

def main():
    parser=argparse.ArgumentParser(description='迁移旧版求职状态与模拟面试记录，不覆盖目标已有文件')
    parser.add_argument('source',type=Path)
    args=parser.parse_args();root=Path(__file__).resolve().parent;source=args.source.resolve()
    if not source.is_dir():parser.error('源版本目录不存在')
    copied=skipped=0
    for folder,prefix in [('applications','job'),('mock_interviews','mock')]:
        target=root/'data'/folder;target.mkdir(parents=True,exist_ok=True)
        for path in (source/'data'/folder).glob(prefix+'_*.json'):
            safe_id(path.stem,prefix)
            value=json.loads(path.read_text(encoding='utf-8-sig'))
            if folder=='applications':
                state=ApplicationState.from_dict(value)
                if state.application_id!=path.stem:raise ValueError('记录 ID 与文件名不一致：'+path.name)
            else:
                if value['session_id']!=path.stem:raise ValueError('会话 ID 不一致：'+path.name)
                safe_id(value['application_id'],'job')
            dest=target/path.name
            if dest.exists():skipped+=1;continue
            shutil.copy2(path,dest);copied+=1
    print(f'已迁移 {copied} 个状态文件，保留 {skipped} 个目标已有文件。')
    print('旧版基础材料正文已保存在岗位状态中；可以直接查看，公共基础材料需在网页上传。')
if __name__=='__main__':main()
