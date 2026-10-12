本次页面补丁的更新、迁移与验收步骤见 `V1.8.6.1_RELEASE_NOTES.md`。

# Career Agent V1.8.6.1

可在本机浏览器使用的个人求职工作台：基础材料 → 岗位分析 → 定制简历 → 面试准备 → 模拟面试。

## 启动

```powershell
python -m pip install -r requirements.txt
python run_web.py --provider qwen
```

浏览器打开 http://127.0.0.1:8000，保持终端打开。网页无需 Node 构建，不要直接双击 HTML。

离线验证：`python run_web.py --provider mock`，使用 examples 中的样例文件。

[完整启动、迁移和验收说明](V1.8.6.1_RELEASE_NOTES.md) · [接口契约](WEB_API.md) · [历史 CLI 说明](README_LEGACY.md)

## 迁移旧版

```powershell
python migrate_state.py "..\career-agent-v1.7.2.2"
```

复制旧求职状态和面试会话，不覆盖目标已有文件。不复制 API Key；沿用终端环境变量或自行配置本机 .env。

## 验证

```powershell
python -m unittest discover -s tests -v
```

本版仅监听本机，无公开部署或多用户登录。不要多个网页服务／旧 CLI 同时写入同一状态目录。生成内容会发送材料给所选模型服务。用户手动修改稿独立保存，尚未自动重新审查。
