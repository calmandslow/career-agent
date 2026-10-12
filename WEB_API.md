# 网页接口契约

由 `run_web.py` 启动的 `web_app.create_app` 提供接口，前端与接口同源。不使用旧 `app.py` 的同步文本接口。无 CORS 跨站开放；本版只适用于 localhost 单用户。

| 方法 | 路径 | 输入／返回 |
| --- | --- | --- |
| GET | /api/config | 当前 provider、模型路由、Key 是否存在（不返回 Key） |
| GET | /api/materials | 材料元数据、状态和提取文字 |
| POST | /api/materials | multipart：file、kind=resume/portfolio/jd、JD 的 application_id；返回 material 与 task |
| POST | /api/materials/{id}/deactivate | 移出默认材料／岗位 JD，不删除旧快照 |
| GET / POST | /api/applications | 列表／创建，company 与 role 可选 |
| GET / PATCH | /api/applications/{id} | 读取完整岗位状态／修正公司与岗位 |
| POST | /api/applications/{id}/bind-materials | 绑定当前默认基础材料，生成材料引用与正文快照 |
| PUT | /api/applications/{id}/resume-draft | text；独立保存用户修改稿，标记未重新审查 |
| POST | /api/applications/{id}/approve | 确认待审结果；硬约束不合格不提交，答案还需要语义审查通过 |
| POST | /api/applications/{id}/tasks | action=analyze/resume/questions/answers/review_answers/update_answer；后者需要 question_index（从0计）和 feedback |
| GET | /api/tasks | 所有持久任务，可用 application_id 过滤 |
| GET | /api/tasks/{id} | 状态、日志、结果、错误和成本摘要 |
| POST | /api/tasks/{id}/retry | 仅失败／中断任务可续跑；同一旧任务重复重试返回已有后继任务 |
| GET / POST | /api/applications/{id}/sessions | 历史列表／新建面试（count=1..10） |
| GET | /api/applications/{id}/sessions/{sid} | 会话快照、全部主问题与追问、回答、教练反馈 |
| POST | /api/applications/{id}/sessions/{sid}/actions | operation=answer/skip/finish/resume/feedback；answer 提交正文 |
| GET | /api/applications/{id}/download/{kind} | kind=resume/draft/answers/state/session/transcript/feedback；最后三项需 session_id |

任务状态：queued → running → completed / waiting_for_user / failed。启动时未完成任务改为 interrupted；付费调用不自动重放。网页约每2秒轮询，不应因刷新重新提交 POST。

同一岗位任务期间的重复启动与修改返回 409。不存在记录返回 404，无效输入返回 422。单文件 25MB。图片 OCR 由原执行器完成；模型连接、额度与鉴权错误沿用分类与重试策略。

费用是本地价格配置的估算；失败请求、未知 usage、未匹配价格不能当作零成本。任务结果不等于语义质量保证。

## V1.8.1 岗位人工修正

`PUT /api/applications/{application_id}/jd-profile`，请求体为完整 JDProfile（与 GET 返回的 jd_profile 一致）。需先完成分析，运行期间拒绝修改。必填岗位名称和概述不得为空。改变分析会清除依赖输出、保留原 JD 和历史会话，并记录前后版本；不改变分析不清除。

## V1.8.2 前端状态

接口及请求参数未变化。任务 action、application_id、payload.session_id 决定页面归属；日志用于显示正在执行的具体生成/审查步骤。每两秒查询任务，结束后刷新保存结果；仅查看页面不会新建模型请求。历史任务的 provider 保持原值。

## V1.8.3 可恢复删除

- `DELETE /api/applications/{application_id}`：移出正常岗位列表，重复删除幂等；有运行任务返回 409。
- `GET /api/deleted-applications`：已删除岗位列表。
- `POST /api/deleted-applications/{application_id}/restore`：恢复原状态及关联数据；已有同编号岗位时拒绝覆盖。

删除岗位的任务和 JD 在正常列表隐藏；基础材料保持原状。保留文件用于恢复，不是永久数据清除。

## V1.8.4 逐题确认

`GET /api/applications/{id}` 增加 answer_hashes、answer_confirmed。
`POST /api/applications/{id}/confirm-answer` 接收 question_index（从 0 开始）与 answer_hash（当前 GET 返回值）。内容变化返回 409，未通过内容检查返回 review_required，本地检查失败返回 needs_changes。确认按题和版本保存；全部待确认答案通过后调用原整体校验。确认操作不调用模型。


## V1.8.6：追问边界与恢复

会话决策增加 answer_quality（complete/needs_more/unable/not_evaluated）、follow_up_kind（supplement/extension/none）、rationale。回答充分仍可有明确目的的延伸追问；原有会话兼容。decision_candidate 与 decision_attempts 保存校验前候选及诊断记录。

POST /api/applications/{id}/sessions/{session_id}/actions 原操作入口支持 operation=move_on：在 evaluate 阶段保留已提交主问题回答，进入下一题；最后一题仍会生成反馈。任务保存 expected_turn_count，旧请求不能跳过后续新回答。不改变用户回答的 response_status，也不构造新的答题记录。
