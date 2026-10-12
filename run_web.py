"""Launch the integrated local product, keeping keys in the process environment."""
import argparse
import os
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description='Career Agent 本地工作台')
    parser.add_argument('--provider',choices=['qwen','mock'],default='qwen')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    # Optional local .env; current shell variables take precedence. No dependency needed.
    env=Path(__file__).resolve().parent/'.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line:continue
            key,value=line.split('=',1);key=key.strip();value=value.strip()
            if len(value)>1 and value[0]==value[-1] and value[0] in "\"'":value=value[1:-1]
            if key.replace('_','').isalnum():os.environ.setdefault(key,value)
    import uvicorn
    from web_app import create_app
    print(f'Career Agent · {args.provider} · http://127.0.0.1:{args.port}')
    print('保持此窗口打开。按 Ctrl+C 停止；请勿同时用旧版 CLI 操作相同状态。')
    uvicorn.run(create_app(provider=args.provider),host='127.0.0.1',port=args.port,workers=1)

if __name__=='__main__':main()
