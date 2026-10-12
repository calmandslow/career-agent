# V1.7.2.2：逐题反馈覆盖与缺失项续补

请先阅读 [V1.7.2.2_RELEASE_NOTES.md](V1.7.2.2_RELEASE_NOTES.md)。本轮不接UI。支持只重新生成旧会话的反馈，不重复提问。

# V1.7.1.4

请先阅读 [V1.7.1.4_RELEASE_NOTES.md](V1.7.1.4_RELEASE_NOTES.md)，下方为历史版本说明。

# V1.7.1.3：经历与假设回答分离、按题优化

本版在执行 Answer Coach Skill 时加载 `config/answer_coach_policy.md` 的补充政策。
明确的生成政策覆盖旧 Skill 中“证据不足一律留空”的冲突规则，无需修改或安装个人 Skill。

## 本轮先验证第3题

解压到桌面 career-agent-v1.7.1.3，然后从 V1.7.1.2 复制最新状态文件：

```powershell
cd "C:\Users\lwt\Desktop\career-agent-v1.7.1.3"
New-Item -ItemType Directory -Force ".\data\applications" | Out-Null
Copy-Item "..\career-agent-v1.7.1.2\data\applications\job_38182b62f385.json" ".\data\applications\"
python -m unittest discover -s tests -v
```

单题生成使用当前 Qwen 模型配置；沿用同账户 API Key。如果配置放在旧 .env，可自行复制到新目录。先用 show_model_routes.py 核对实际模型，不会调用 API。

```powershell
python show_model_routes.py
python qwen_update_answer.py job_38182b62f385 --question 3 --feedback "这是一个假设情境题。保留我没有真实跨团队协调经历的事实；明确标注假设处理思路，讲清业务目标、技术成本、方案取舍、验收与风险，不能编造过去经历或保证成功。"
```

此命令只请求第3题，不重跑其他 Skill；网络重试仍可能产生多个请求。
更新后的四题保存在 pending_interview_answers.md，原答案留在状态历史中。
如果其他题触发新规则（例如第2题缺少实验周期），整份输出保持待审，这是预期行为。
日志/成本报告写到 outputs/<id>/answer_updates/<update-id>/，不覆盖旧报告。

通过 --edited-json PATH 可提交一份 PreparedAnswer JSON，完全不调用 API。
普通 --feedback 使用模型。更新成功不等于人工批准；内容验收后再 --approve。
新字段 answer_mode 为 experience/hypothetical/mixed；旧状态缺少字段时以 auto 兼容。

## 下一题反馈建议

- 第1题：明确哪些仅为工作流设计，不把规划写成实现或上线。
- 第2题：用假设 A/B 业务案例解释方案、指标定义、分流、样本量、SRM、周期、停止规则和上线决策，不虚构基线数据。
- 第4题：收紧复杂交互和绝对质量承诺；补充Visio流程分支与异常路径练习。

## 验证边界

73项本地测试包含假设题允许回答、经历题禁止编造、单题不影响其他题、请求失败保留答案和成本记录。
题型推断和质量检查仍是有限的规则；不能证明事实支撑或语义质量。未执行真实模型调用。

---

# V1.7.1.2：Answer Coach 校验与待审输出修复

本补丁不重新生成整批答案，不自动将本地规则通过等同于真实性审查通过。

## 零 API 验证

将原版本 data/applications 中的状态 JSON 复制到本版本同名目录，然后运行：

```powershell
python -m unittest discover -s tests -v
python recheck_answer_coach.py job_38182b62f385
```

第二条仅做 Python 本地校验，输出 answer_local_recheck.json、pending_human_review.json 和 pending_interview_answers.md。若显示 local_validation_passed，表示规则校验通过，仍待人工审查。

阅读待审答案，确认接受后运行（仍不调用 API）：

```powershell
python recheck_answer_coach.py job_38182b62f385 --approve
```

正式输出 answer_prep_profile.json、interview_answers.md。旧 run_report.json 保留原运行事实，本次本地复核记录在 answer_local_recheck.json，不覆盖原成本报告。

重复执行 qwen_answer_coach.py 会保留待审或已完成答案；明确需要花费额度重新生成时才使用 --force。

校验仍为范围有限的 Python 规则，不能代替语义真实性审查。ABTest 的周期/停止规则、第3题假设回答以及表达优化将在单题内容迭代中处理。

---

# Career Agent V1.7.1.1

Current package: **V1.7.1.1 targeted Answer Coach repair**.

The active product path has six preparation skills:

`JD analysis → resume tailoring → interview research → questions → answer coaching → mock interview`

The earlier `interview-review` implementation remains available for regression
testing, but real-interview review is deferred from the current product scope.

Python owns planning, state, validation, pause/resume behavior, and orchestration.
An interchangeable executor owns model calls. The default executor is local Mock,
so the complete workflow runs without an API key or cost.

## Current implementation order

1. **V1.5.1:** model routing and mandatory semantic truthfulness verification.
2. **V1.6.2:** progress output, reliability, cost logs, classified errors, conservative truthfulness repair, and human-review continuation.
3. **V1.7.1:** real evidence-bounded Answer Coach for P0 interview questions.
4. **V1.7.1.1:** rejects unsupported time promises, strengthens scenario and
   ABTest answers, and repairs only the failed answer instead of regenerating
   the complete answer batch.
4. **Next V1.7.2:** live, one-question-at-a-time adaptive Mock Interview.

## Run the complete Mock Harness

```bash
cd career-agent-v1
python demo.py
python -m unittest discover -s tests -v
```

The demo executes six preparation skills, adds an interview record, and then
executes `interview-review`, covering all seven skill/state handoffs.

## API

```bash
python -m pip install -r requirements.txt
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000/docs`. Mock is the default; no environment setup is
required. Important endpoints:

- `POST /applications`
- `PATCH /applications/{application_id}/materials`
- `POST /applications/{application_id}/plan`
- `POST /applications/{application_id}/run`
- `GET /applications/{application_id}`

Example run body:

```json
{"intent": "mock_interview"}
```

The provider boundary is `SkillExecutor.execute(skill, context, output_model)`.
Future Chinese/OpenAI adapters implement this same contract, so Planner,
Orchestrator, Skills, Verifier, and State remain unchanged.

## Run one real Qwen skill

The Qwen adapter uses Alibaba Cloud Model Studio's OpenAI-compatible API and
keeps the same Pydantic output contract as the Mock executor.

In a new PowerShell window, confirm that the API key is available, configure
the two model tiers, then run:

```powershell
cd C:\Users\lwt\Desktop\career-agent-v1-mock-harness
$env:LLM_PROVIDER="qwen"
$env:QWEN_FAST_MODEL="qwen3.8-flash"
$env:QWEN_CORE_MODEL="qwen3.7-plus"
python qwen_smoke_test.py
```

If the current PowerShell session still contains the old global model setting,
remove it before testing model routing:

```powershell
Remove-Item Env:QWEN_MODEL -ErrorAction SilentlyContinue
python show_model_routes.py
```

`QWEN_MODEL` remains supported as a deliberate global override. When it is set
and the two tier-specific variables are absent, every skill uses that model.

If the API console gives you a workspace-specific compatible-mode URL, set it
before running:

```powershell
$env:DASHSCOPE_BASE_URL="paste-the-compatible-mode-base-url-here"
```

Never put the real API key in `.env.example`, source code, screenshots, or Git.

## Run the real preparation pipeline

After `qwen_smoke_test.py` succeeds, run:

```powershell
python qwen_pipeline_test.py
```

This executes four core skills through the existing Orchestrator:

`jd-analyzer → resume-tailor → interview-research → interview-question-generator`

V1.5.1 always makes a semantic-review call after Resume Tailor. If that review
finds an overstated or unsupported claim, Plus receives one repair call and the
repaired result is force-reviewed again. Therefore the exact call count depends
on the generated wording.

It verifies cross-skill context handoff, Pydantic structured outputs, Verifier
checks, State persistence, and final question generation. The script uses a
temporary state directory, so its sample data does not enter the normal
`data/applications` directory.

## Run with real files

The material pipeline accepts `txt`, `md`, `pdf`, `docx`, `pptx`, `png`,
`jpg`, `jpeg`, and `webp`. Repeat `--jd` for ordered JD screenshots and repeat
`--portfolio` for multiple portfolio files.

```powershell
python qwen_material_pipeline.py `
  --jd "C:\path\to\jd_01.png" `
  --jd "C:\path\to\jd_02.png" `
  --resume "C:\path\to\resume.pdf" `
  --portfolio "C:\path\to\pickbetter.md" `
  --company "Company Name" `
  --role "AI Product Manager"
```

Image materials are transcribed by the selected multimodal Qwen model before
the normal pipeline runs. Results are written to
`outputs/<application_id>/result.json`. V1.6 also writes `run_report.json`,
`execution_log.jsonl`, and `cost_report.md`. Real inputs, outputs, and
application state JSON files are ignored by Git.

## V1.4 evidence-aware context

Before resume tailoring, the Harness converts the resume and portfolio into
source-addressable evidence chunks. The full resume is preserved, while long
portfolio content is ranked against the JD and capped at 24,000 characters.
Every supported strength and materially rewritten resume claim must cite an
existing `RESUME-*` or `PORTFOLIO-*` ID. The Verifier rejects unknown IDs.

The generated `material_evidence.stats` records the original and selected
portfolio character counts so context compression can be inspected after a run.

## V1.4.2 resume quality contract

Resume Tailor now follows the JD language and produces a complete structured
resume with identity/contact details (when present), target role, professional
summary, named entries, dates/locations, and evidence-backed bullets. It keeps
`completed`, `observed`, `designed`, and `planned` claims distinct, applies a
conservative fit level, and rejects common unsupported proficiency/evaluative
phrases. Each generated claim is also written to `resume_evidence_audit.md` so
the wording, status, and source IDs can be reviewed before an application.

### V1.4.2.1 verifier hotfix

Claim-audit coverage now tolerates minor wording and punctuation differences
when a unique audit record has the same evidence IDs and achievement status.
The verifier still requires one audit record per resume claim and rejects
missing records or unknown evidence IDs.

## V1.4.3 final-output verification

The resume schema now rejects empty sections and entries without bullets. The
Verifier requires education, experience/project, and skills content; requires
one bidirectional audit record for every contact item, summary, and bullet; and
rejects `identity` claim types attached to non-identity statuses. If the first
generated result fails verification, the Harness sends the exact validation
feedback and previous output back to the same model for one repair attempt.
A second failure pauses with the candidate output and both validation issues
retained in State for human review instead of spending more API calls.

## V1.4.3.1 resume rendering polish

The deterministic Markdown renderer suppresses a duplicated date when a model
places the same value in both `subheading` and `date_or_location`. The Resume
Tailor instructions now reserve date/location for one field and require
design-stage personal projects to be described as planning/design practice,
not unqualified company-product experience. Use `render_saved_resume.py` to
apply renderer fixes to a saved successful State without another API call.

## V1.5.1 mandatory semantic review coverage

Qwen calls are now routed by skill rather than sharing one global model:

- Flash tier: image OCR, JD Analyzer, Interview Research, and the internal
  Resume Claim Verifier.
- Plus tier: Resume Tailor, Interview Question Generator, Answer Coach, Mock
  Interview, and the deferred Interview Review skill.

The Resume Claim Verifier is an internal Harness quality-control skill, not a
new user-facing workflow step. Professional summary, skills, fit summary,
matched strengths, and candidate positioning are always reviewed. Project
bullets are added when they contain risky wording or carry `designed`,
`planned`, or `observed` status. The verifier compares those claims only with
their cited candidate evidence and classifies them as `supported`, `ambiguous`,
`overstated`, or `unsupported`.

- `overstated` or `unsupported`: send the exact feedback and previous output to
  Resume Tailor for one Plus repair, then force-review every resume bullet plus
  the mandatory candidate-assessment claims.
- `ambiguous`: pause immediately for human clarification.
- second failure: pause for human review instead of making more repair calls.

Every semantic review round is retained in State and rendered to
`outputs/<application_id>/resume_semantic_review.md`. The original evidence
audit remains in `resume_evidence_audit.md`.

If the Flash quota is unavailable, keep the routing boundary but point both
tiers to Plus:

```powershell
$env:QWEN_FAST_MODEL="qwen3.7-plus"
$env:QWEN_CORE_MODEL="qwen3.7-plus"
```

## V1.6 reliability, progress, and cost logs

Every real API attempt now records the skill, call type, model, attempt number,
status, elapsed time, token usage, error class, and HTTP status code when
available. Prompts, candidate materials, API keys, and provider error messages
are not copied into the execution log.

The CLI shows step progress, model-call progress, retry decisions, and a waiting
heartbeat every 15 seconds by default. This makes a slow model response visibly
different from a stopped process.

Retry policy:

- retry connection, timeout, rate-limit, HTTP 408/409/429, and 5xx failures;
- do not retry authentication, permission, invalid-request, or exhausted-quota
  failures;
- use exponential backoff and keep every attempt in State before propagating a
  final failure.

Operational failures are stored with a safe `error_category`: `dns`,
`connection`, `timeout`, `rate_limit`, `server`, `authentication`, `permission`,
`quota_exhausted`, `invalid_request`, or `unknown`. DNS and other transient
categories retry; authentication, permission, exhausted quota, and invalid
requests stop immediately with a clear CLI action message.

Configuration:

```powershell
$env:LLM_TIMEOUT_SECONDS="180"
$env:LLM_MAX_RETRIES="2"
$env:LLM_RETRY_BASE_SECONDS="2"
$env:LLM_RETRY_MAX_SECONDS="8"
$env:PROGRESS_HEARTBEAT_SECONDS="15"
```

After a successful or failed real run, inspect:

```text
outputs/<application_id>/run_report.json
outputs/<application_id>/execution_log.jsonl
outputs/<application_id>/cost_report.md
```

`config/model_pricing.json` contains editable CNY list-price tiers. The bundled
values cover Qwen3.7 Plus and Qwen3.8 Flash for the mainland endpoint. Cost is a
list-price estimate: free quota, temporary discounts, caching discounts, and
Token Plans are not deducted. Use `MODEL_PRICING_PATH` to point to an updated or
account-specific price table.

## Resume a failed real pipeline

Transient connection failures retain application State. Resume only the missing
steps without reading images or repeating completed model calls:

```powershell
python qwen_resume_pipeline.py job_38182b62f385
```

Qwen calls retry transient connection, timeout, rate-limit, and server errors
twice by default. Override this with `LLM_MAX_RETRIES` when needed. The resumed
run receives a new `run_id`, and its report includes only calls made in that
resume attempt; `execution_log.jsonl` preserves the complete application history.

To validate a new Resume Tailor version with an existing State using only one
model call, copy the retained application JSON into this version's
`data/applications` directory and run:

```powershell
python qwen_rerun_resume_tailor.py job_38182b62f385
```

This intentionally invalidates resume-tailor and downstream derived outputs in
that copied State. It writes both `resume_tailor_result.json` and a readable
`tailored_resume.md`, plus `resume_evidence_audit.md` and
`resume_semantic_review.md`.

If two semantic-review rounds still leave unsafe or ambiguous wording, the run
pauses at `waiting_for_human_review_resume-tailor`. Review
`outputs/<application_id>/pending_human_review.json`, then either approve it as-is:

```powershell
python qwen_resolve_human_review.py job_38182b62f385 --approve
```

or commit an edited `ResumeTailorOutput` JSON:

```powershell
python qwen_resolve_human_review.py job_38182b62f385 --edited-json .\reviewed_resume.json
```

For a pending result that only contains the known personal-project wording
problem, apply the conservative Harness rewrite and commit it without an API call:

```powershell
python qwen_resolve_human_review.py job_38182b62f385 --apply-conservative
```

This continuation performs schema/evidence validation and writes final files,
but makes no model API call. V1.6.2 also adds a conservative deterministic pass
for personal/design-stage work so wording such as `负责`, `落地`, `Owner`, and
unqualified `经验` is not allowed to silently upgrade planning practice into
professional delivery experience.
