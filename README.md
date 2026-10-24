# Career Agent V2.0

V2.0 将 V1.9 的职业证据、动态招聘流程和准备度封装为一个可持续对话的 Agent。统一输入框可以处理求职咨询、招聘进展、生成任务、专项训练、完整模拟和普通问答；所有职业数据写入都通过可检查的确认提案完成。

[V2.0 更新与验收说明](V2.0_RELEASE_NOTES.md) · [接口契约](WEB_API.md) · [V1.9 说明](V1.9_RELEASE_NOTES.md)

可在本机浏览器使用的个人求职工作台：基础材料 → 岗位分析 → 定制简历 → 面试准备 → 模拟面试。

## 启动

```powershell
python -m pip install -r requirements.txt
python run_web.py --provider qwen
```

浏览器打开 http://127.0.0.1:8000，保持终端打开。网页无需 Node 构建，不要直接双击 HTML。

离线验证：`python run_web.py --provider mock`，使用 `examples/v20` 中的样例对话。Mock 用于验证路由、计划和数据边界，不代表真实模型回答质量。

[历史 CLI 说明](README_LEGACY.md)

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
