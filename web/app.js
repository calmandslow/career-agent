'use strict';
const $=s=>document.querySelector(s), root=$('#career-proto'), content=$('#content');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={analyze:'岗位分析',resume:'定制简历',questions:'生成问题',answers:'生成准备答案',review_answers:'语义审查',update_answer:'单题更新',interview:'模拟面试',parse_material:'材料解析'};
const statusNames={created:'新建',completed:'已完成',queued:'排队中',running:'执行中',failed:'失败，可续跑',interrupted:'已中断，可续跑',waiting_for_user:'待补充或确认',ready:'已解析',parsing:'解析中',awaiting_answer:'等待回答',evaluate:'判断追问中',advance:'切换问题中',summary:'生成反馈中'};
const S={page:'new',tab:'overview',id:localStorage.getItem('career-current-job'),app:null,jobs:[],deleted:[],materials:[],evidence:{items:[],changes:[]},tasks:[],sessions:[],session:null,config:null,agentThread:{mode:'assistant',messages:[],pending_proposal:null,context:{scope:'global'}},agentPending:null,locked:false,drafts:{},jdDrafts:{},answers:{},answerFeedback:{},seen:{},polling:false,notice:null,operationScope:null,upload:null};
const pageScope=()=>S.page==='workspace'?`${S.page}:${S.id}:${S.tab}:${S.tab==='mock'?S.session?.session_id||'none':''}`:S.page;
const activeTask=t=>['queued','running'].includes(t.status);
const busy=()=>S.locked||S.tasks.some(t=>activeTask(t)&&(S.page==='workspace'?t.application_id===S.id:S.page==='materials'?t.application_id===null:false));
const taskContext=t=>{const job=S.jobs.find(j=>j.application_id===t.application_id);return t.application_id?`${job?.company||'待识别公司'} / ${job?.role||'待识别岗位'}`:'基础材料';};
function taskDetails(t){return `<details data-task-details="${t.task_id}"><summary>执行详情与费用</summary><p class="small">${esc(taskContext(t))} · ${t.provider==='mock'?'离线 Mock':'真实模型'} · ${esc(t.created_at?new Date(t.created_at).toLocaleString():'')}</p>${t.cost?`<p class="small">${t.cost.total_tokens} Tokens · ${t.cost.completed_calls} 次成功调用 · ${t.cost.retries} 次重试 · 目录价估算 ¥${Number(t.cost.estimated_cost_cny||0).toFixed(6)}（以账单为准；失败或未匹配价格的调用可能低估）</p>`:''}<div class="log">${(t.logs||[]).map(l=>`<div>${esc(l.message)}</div>`).join('')||'暂无执行日志。'}</div></details>`;}
const taskView=t=>`<div class="task" data-task-id="${t.task_id}"><div class="row"><strong>${esc(labels[t.action]||t.action)} · ${esc(taskContext(t))}</strong><span>${t.retried_as?'已续跑 · 历史记录':esc(statusNames[t.status]||t.status)}</span></div>${t.error?`<p class="danger">${esc(t.error)}</p>`:''}${taskDetails(t)}${['failed','interrupted'].includes(t.status)&&!t.retried_as?`<button data-retry="${t.task_id}">继续这个任务</button>`:''}</div>`;
// Polling updates data, not the user's reading position. Keep unchanged cards
// in place and preserve both document and independently scrollable log views.
function preserveTaskReading(target,update){
 const x=window.scrollX,y=window.scrollY;
 const anchor=Array.from(target.querySelectorAll('[data-task-id], [data-task-details]')).find(el=>{const r=el.getBoundingClientRect();return r.bottom>0&&r.top<window.innerHeight;});
 const key=anchor?.dataset.taskId||anchor?.dataset.taskDetails,top=anchor?.getBoundingClientRect().top;
 const views=new Map(Array.from(target.querySelectorAll('[data-task-details]'),d=>{const log=d.querySelector('.log');return [d.dataset.taskDetails,{open:d.open,top:log?.scrollTop||0,left:log?.scrollLeft||0,focused:d.querySelector('summary')===document.activeElement}];}));
 update();
 target.querySelectorAll('[data-task-details]').forEach(d=>{const v=views.get(d.dataset.taskDetails);if(!v)return;d.open=v.open;const log=d.querySelector('.log');if(log){log.scrollTop=v.top;log.scrollLeft=v.left;}if(v.focused)d.querySelector('summary')?.focus({preventScroll:true});});
 const next=key?Array.from(target.querySelectorAll('[data-task-id], [data-task-details]')).find(el=>(el.dataset.taskId||el.dataset.taskDetails)===key):null;
 window.scrollTo({left:x,top:next?window.scrollY+next.getBoundingClientRect().top-top:y,behavior:'instant'});
}
function renderTasks(){
 if(!content.querySelector('#task-list'))content.innerHTML='<h1>运行记录</h1><p class="minor">记录所有岗位的任务；详情与费用默认收起。</p><div id="task-list"></div>';
 const target=$('#task-list');
 preserveTaskReading(target,()=>{
  const old=new Map(Array.from(target.querySelectorAll('[data-task-id]'),el=>[el.dataset.taskId,el]));
  let cursor=target.firstElementChild;
  for(const t of S.tasks){const html=taskView(t);let node=old.get(t.task_id);old.delete(t.task_id);
   if(!node||node._taskHTML!==html){const template=document.createElement('template');template.innerHTML=html;const fresh=template.content.firstElementChild;fresh._taskHTML=html;
    if(node){if(cursor===node)cursor=fresh;node.replaceWith(fresh);}node=fresh;
   }
   if(node!==cursor)target.insertBefore(node,cursor);cursor=node.nextElementSibling;
  }
  old.forEach(node=>node.remove());
  target.querySelector('.empty-tasks')?.remove();
  if(!S.tasks.length)target.innerHTML='<p class="empty-tasks">还没有运行记录。</p>';
 });
}
const pageActions={overview:['analyze'],resume:['resume'],prep:['questions','answers','review_answers','update_answer'],mock:['interview']};
const skillPage={'jd-analyzer':'overview','resume-tailor':'resume','resume-claim-verifier':'resume','interview-research':'prep','interview-question-generator':'prep','interview-answer-coach':'prep','answer-semantic-verifier':'prep','mock-interview':'mock'};
function taskSkill(t){let skill='';for(const {message} of t.logs||[]){const m=message.match(/^\[\d+\/\d+\] ([\w-]+) started/)||message.match(/^\[LLM\] ([\w-]+) started/);if(m)skill=m[1];if(message.includes('semantic review started'))skill='resume-claim-verifier';}return skill;}
function currentTask(){
 if(S.page==='new'||S.page==='tasks')return null;
 return S.tasks.find(t=>!t.retried_as&&(S.page==='materials'?t.action==='parse_material'&&t.application_id===null:S.page==='workspace'&&t.application_id===S.id&&(pageActions[S.tab]?.includes(t.action)||(S.tab==='overview'&&t.action==='parse_material')||(activeTask(t)&&skillPage[taskSkill(t)]===S.tab))&&(S.tab!=='mock'||(S.session&&t.payload?.session_id===S.session.session_id))))||null;
}
function runningText(t){
 const messages=(t.logs||[]).map(l=>l.message),last=messages.at(-1)||'';
 if(/retrying/.test(last))return '连接暂时中断，正在自动重试';
 if(t.action==='parse_material')return '正在解析材料';
 if(t.action==='interview'){
  const phase=S.session?.session_id===t.payload?.session_id?S.session.phase:null;
  if(phase==='summary'||['feedback','finish'].includes(t.payload?.operation))return '正在生成教练反馈';
  if(phase==='advance')return '正在切换面试问题';
  return '正在判断回答与追问';
 }
 const skill=taskSkill(t);
 const names={'jd-analyzer':'正在分析岗位','resume-tailor':'正在生成定制简历','resume-claim-verifier':'正在审查简历事实与表达','interview-research':'正在准备面试研究','interview-question-generator':'正在生成面试问题','interview-answer-coach':'正在生成或修改准备答案','answer-semantic-verifier':'正在审查面试答案'};
 return names[skill]||({analyze:'正在分析岗位',resume:'正在生成定制简历',questions:'正在生成面试问题',answers:'正在生成准备答案',review_answers:'正在审查面试答案',update_answer:'正在修改这道答案'}[t.action]||'正在处理');
}
function pageState(t){
 if(S.upload?.scope===pageScope())return {title:'正在上传材料',hint:`${S.upload.name}：上传完成后自动解析。`};
 if(t&&activeTask(t))return {title:t.status==='queued'?'任务排队中':runningText(t),hint:'请等待处理完成；你可以切换页面，任务会在后台继续。'};
 if(t&&['failed','interrupted'].includes(t.status))return {title:t.status==='failed'?'本步骤处理失败':'本步骤已中断',hint:t.error||'已保留进度；解决问题后可以继续这个任务。',error:true};
 if(S.page==='materials')return t?{title:t.status==='completed'?'材料解析完成':'材料待处理',hint:'可核对提取文字，再为岗位关联材料。'}:null;
 if(S.page!=='workspace'||!S.app)return null;
 const a=S.app;
 if(t?.result?.status==='semantic_review_pending')return {title:'候选答案需要修改',hint:'候选未通过语义审查，原答案已保留；请查看审查建议后修改。'};
 if(t?.result?.status==='semantic_review_required')return {title:'答案还需要检查',hint:'点击“检查答案内容”，再逐题核对是否符合你的情况。'};
 if(a.pending_validation_output&&S.tab==='prep'&&a.current_stage.includes('resume-tailor'))return {title:'前置简历待人工确认',hint:'请返回定制简历页核对候选结果，确认后继续面试准备。'};
 if(a.pending_validation_output&&((S.tab==='resume'&&a.current_stage.includes('resume-tailor'))||(S.tab==='prep'&&a.current_stage.includes('interview-answer-coach'))))return S.tab==='prep'?{title:'准备答案已生成',hint:'查看下方每道答案，核对后逐题确认；需要调整的话可以填写修改意见。'}:{title:'待你确认',hint:'请核对内容是否符合你的真实经历，再确认或修改。'};
 if(S.tab==='overview')return a.jd_profile?{title:'岗位分析已完成',hint:'请核对分析结果；可人工修正，然后继续定制简历。'}:{title:a.jd?'待分析岗位':'待上传岗位 JD',hint:a.jd?'材料已解析，点击分析岗位。':'先上传截图或文本。'};
 if(S.tab==='resume')return a.tailored_resume?{title:'定制简历已完成',hint:'核对简历内容后，点击下一步进入面试准备。'}:{title:'待生成定制简历',hint:a.base_resume?'点击生成定制简历。':'请先关联本岗位的基础材料。'};
 if(S.tab==='prep')return a.answer_prep_profile?{title:a.answer_review_passed?.length&&a.answer_review_passed.every(Boolean)?'面试答案审查已通过':'准备答案已生成',hint:'核对答案与审查结果，然后进入模拟面试。'}:a.interview_prep_profile?{title:'面试问题已生成',hint:'可生成准备答案，或直接进入模拟面试。'}:{title:'待生成面试问题',hint:'点击生成问题清单；缺少的前置步骤会按需执行。'};
 const session=S.session;
 if(!session)return {title:'待开始模拟面试',hint:'选取主问题数量，再新开一轮。'};
 const phase=session.phase;
 return {title:({awaiting_answer:'等待你的回答',evaluate:'待继续判断追问',advance:'待继续切换问题',summary:'待生成教练反馈',completed:'本轮模拟面试已完成'})[phase]||'待继续面试',hint:phase==='awaiting_answer'?'按真实经历或明确的假设思路回答。':phase==='completed'?'查看逐题教练建议，也可以新开一轮练习。':'回答已保存，可继续待处理步骤。'};
}
const reviewLabels={supported:'依据支持',ambiguous:'需要确认',overstated:'表达过强',unsupported:'依据不足',pass:'通过',revise:'需要修改',human_review:'需要人工确认'};
const resumeReviews=history=>(history||[]).flatMap(h=>h.reviews||[]).map(r=>`<div class="item"><span class="pill">${reviewLabels[r.verdict]||esc(r.verdict)}</span>${prose(r.rationale)}${r.suggested_rewrite?'<h4>建议表述</h4>'+prose(r.suggested_rewrite):''}</div>`).join('')||'<p>尚无审查记录。</p>';
const answerReviews=history=>(history||[]).map(h=>{const r=h.review||h;return `<div class="item"><strong>第 ${(h.question_number??((h.answer_index??0)+1))} 题 · ${reviewLabels[r.verdict]||'已记录'}</strong>${(r.issues||[]).map(i=>`<blockquote>${esc(i.quote)}</blockquote>${prose(i.reason)}${prose(i.suggestion)}`).join('')}</div>`;}).join('')||'<p>尚无审查记录。</p>';
const prose=v=>`<div class="prose">${esc(v)}</div>`;
const list=a=>`<ul>${(a||[]).map(x=>`<li>${esc(typeof x==='string'?x:JSON.stringify(x))}</li>`).join('')}</ul>`;
const download=(kind,label,sid)=>`<a href="/api/applications/${S.id}/download/${kind}${sid?'?session_id='+sid:''}">${label}</a>`;
function clearNotice(){S.notice=null;const el=$('#notice');el.hidden=true;el.textContent='';}
function showNotice(){const el=$('#notice');const n=S.notice;el.hidden=!n||n.scope!==pageScope();if(!el.hidden){el.className=n.error?'error':'notice';el.textContent=n.text;}}
function notify(text,error=false,scope=S.operationScope||pageScope()){S.notice={text,error,scope};showNotice();}
async function api(path,options={}){const {method='GET',body}=options;const response=await fetch('/api'+path,{method,headers:body instanceof FormData?{}:body?{'Content-Type':'application/json'}:{},body:body instanceof FormData?body:body?JSON.stringify(body):undefined});let data;try{data=await response.json();}catch{throw new Error('后端响应异常，请查看终端。');}if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data;}
async function refresh(){
 [S.jobs,S.materials,S.evidence,S.tasks,S.deleted]=await Promise.all([api('/applications'),api('/materials'),api('/evidence'),api('/tasks'),api('/deleted-applications')]);
 if(S.id&&!S.jobs.some(j=>j.application_id===S.id)){S.id=null;S.app=null;localStorage.removeItem('career-current-job');}
 if(S.id){[S.app,S.sessions]=await Promise.all([api('/applications/'+S.id),api('/applications/'+S.id+'/sessions')]);
 if(S.session&&S.session.application_id===S.id)S.session=await api(`/applications/${S.id}/sessions/${S.session.session_id}`);
 else {S.session=null;const remembered=localStorage.getItem('career-session-'+S.id);if(remembered&&S.sessions.some(h=>h.session_id===remembered))S.session=await api(`/applications/${S.id}/sessions/${remembered}`);}
 }
 S.agentThread=await api('/agent/thread'+(S.id?'?application_id='+encodeURIComponent(S.id):''));
 render();
}
function progress(){
 const t=currentTask(),state=pageState(t),target=$('#progress');
 const html=state?`<section class="step-status"><div class="row"><strong>${esc(state.title)}</strong>${t?`<span class="pill">${t.provider==='mock'?'离线 Mock':'真实模型'}</span>`:''}</div><p class="${state.error?'danger':'minor'}">${esc(state.hint)}</p>${t&&state.error?`<button data-retry="${t.task_id}">继续这个任务</button>`:''}${t?taskDetails(t):''}</section>`:'';
 if(target._taskHTML!==html){preserveTaskReading(target,()=>{target.innerHTML=html;});target._taskHTML=html;}
 if(S.page==='workspace'&&state)$('#stage').textContent=state.title;
 showNotice();
}
function materialCard(m){return `<div class="item"><div class="row"><strong>${esc(m.name)} · v${m.version||1}</strong><span class="pill">${esc(statusNames[m.status]||m.status)} · ${m.active?'当前使用':'历史版本'}</span></div><p class="small">${m.characters} 字符 · ${esc(new Date(m.created_at).toLocaleString())}</p>${m.error?prose(m.error):''}${m.text?`<details><summary>查看提取文字</summary><div class="preview">${prose(m.text)}</div></details>`:''}${m.active?`<button data-deactivate="${m.material_id}">移出默认材料</button>`:''}</div>`;}
function materialsPanel(kind){const items=S.materials.filter(m=>m.kind===kind&&!m.application_id);return `<section class="panel"><h2>${kind==='resume'?'基础简历':'作品集'}</h2><p class="minor">${kind==='resume'?'上传新简历后成为默认版本；旧岗位仍使用原快照。':'可上传多份；已有岗位需明确关联才能使用新材料。'}单文件 ≤25 MB。</p><input aria-label="上传${kind==='resume'?'基础简历':'作品集'}" data-upload="${kind}" type="file" accept=".pdf,.docx,.pptx,.txt,.md,.png,.jpg,.jpeg,.webp" ${kind==='portfolio'?'multiple':''} ${S.locked?'disabled':''}><div class="file-list">${items.filter(m=>m.active).map(materialCard).join('')||'<p>还没有当前材料。</p>'}${items.some(m=>!m.active)?`<details class="material-history"><summary>历史材料（${items.filter(m=>!m.active).length}）</summary>${items.filter(m=>!m.active).map(materialCard).join('')}</details>`:''}</div></section>`;}
const evidenceStatus={pending:'待确认',confirmed:'已确认',rejected:'不使用'};
function renderEvidence(){
 const items=S.evidence.items||[],changes=S.evidence.changes||[];
 content.innerHTML=`<div class="row"><div><h1>职业证据库</h1><p class="minor">从基础材料提取的原文证据。只有你确认过的事实才适合用于关键经历表达。</p></div><span class="badge">${items.filter(i=>i.confirmation_status==='pending').length} 条待确认</span></div><div class="evidence-layout"><section><label>搜索证据<input id="evidence-search" placeholder="例如：增长、A/B测试、跨团队"></label><div id="evidence-list">${items.map(evidenceCard).join('')||'<p>上传并解析基础简历或作品集后，会在这里生成证据。</p>'}</div></section><aside><section class="panel"><h3>最近材料变化</h3>${changes.slice(0,5).map(c=>`<div class="item"><strong>${esc(c.kind==='resume'?'基础简历':'作品集')} v${c.source_version}</strong><p class="small">新增 ${c.added_count} 条 · 移除 ${c.removed_count} 条</p></div>`).join('')||'<p class="small">还没有版本变化。</p>'}</section></aside></div>`;
}
function evidenceCard(item){return `<article class="evidence-card" data-evidence-card><div class="row"><strong>${esc(item.source)} · v${item.source_version}</strong><span class="status-${esc(item.confirmation_status)}">${esc(evidenceStatus[item.confirmation_status]||item.confirmation_status)}</span></div>${prose(item.text)}<p class="small">${esc(item.evidence_id)}</p><div class="actions">${item.confirmation_status!=='confirmed'?`<button data-evidence-status="confirmed" data-evidence-id="${item.evidence_id}">确认事实</button>`:''}${item.confirmation_status!=='rejected'?`<button data-evidence-status="rejected" data-evidence-id="${item.evidence_id}">不使用</button>`:''}</div></article>`;}
function renderAgent(){
 const panel=$('#agent-panel'),thread=$('#agent-thread'),data=S.agentThread||{mode:'assistant',messages:[]};panel.hidden=false;panel.parentElement.classList.remove('single');
 const modeLabels={assistant:'助理模式',focused_training:'专项训练',mock_interview:'面试官模式',general_chat:'普通对话'};$('#agent-mode').textContent=modeLabels[data.mode]||'助理模式';
 const context=data.context||{scope:'global'},contextTarget=$('#agent-context');
 contextTarget.innerHTML=context.scope==='application'?`<div class="agent-context-title"><span>当前岗位记忆</span><strong>${esc(context.company||'待识别公司')} · ${esc(context.role||'待识别岗位')}</strong></div><div class="agent-context-facts"><span>${context.current_stage?'当前：'+esc(context.current_stage):'尚未设置轮次'}</span><span>准备度 ${Number(context.readiness||0)}%</span>${context.next_action?`<span>下一步：${esc(context.next_action)}</span>`:''}</div>${context.risks?.length?`<div class="agent-context-risks">${context.risks.map(r=>`<span>${esc(r)}</span>`).join('')}</div>`:''}`:`<div class="agent-context-title"><span>通用对话</span><strong>未绑定岗位</strong></div><p class="small">打开一个岗位后，我会自动切换到该岗位的独立记忆。</p>`;
 const messages=(data.messages||[]).map(m=>`<div class="message ${m.role==='user'?'user':'assistant'}"><div class="message-meta">${m.role==='user'?'你':modeLabels[m.mode]||'Agent'}</div>${prose(m.content)}${m.plan?.length?`<details><summary>执行计划</summary>${m.plan.map((step,i)=>`<div class="agent-step"><span>${i+1}</span><div><strong>${esc(step.tool)}</strong><p class="small">${esc(step.reason)}</p></div></div>`).join('')}</details>`:''}${m.citations?.length?`<details><summary>引用证据（${m.citations.length}）</summary>${m.citations.map(c=>`<div class="citation"><code>${esc(c.evidence_id)}</code>${prose(c.text)}<span class="small">${esc(c.source)} · v${c.source_version}</span></div>`).join('')}</details>`:''}${m.suggested_prompts?.length?`<div class="agent-suggestions">${m.suggested_prompts.map(x=>`<button type="button" data-agent-prompt="${esc(x)}">${esc(x)}</button>`).join('')}</div>`:''}</div>`).join('');
 const pendingMessage=S.agentPending?`<div class="message user optimistic"><div class="message-meta">你</div>${prose(S.agentPending.text)}</div><div class="message assistant thinking" aria-live="polite"><div class="message-meta">Agent · ${esc(S.agentPending.contextLabel)}</div><div class="thinking-row"><span></span><span></span><span></span><em>${esc(S.agentPending.status)}</em></div></div>`:'';
 const p=data.pending_proposal;
 const proposal=p?`<div class="agent-proposal"><div class="row"><strong>等待你确认</strong><span class="pill">${esc(p.kind)}</span></div><p>${esc(p.summary)}</p>${p.payload?.risks?.length?`<p class="small">风险：${esc(p.payload.risks.join('、'))}</p>`:''}<div class="agent-actions"><button class="primary" id="confirm-progress" data-proposal-id="${p.proposal_id}">确认执行</button><button id="cancel-progress" data-proposal-id="${p.proposal_id}">取消</button></div></div>`:'';
 thread.innerHTML=messages+pendingMessage+proposal||`<div class="agent-empty"><strong>${context.scope==='application'?'已切换到当前岗位':'你可以直接描述目标'}</strong><p class="small">${context.scope==='application'?`接下来默认使用“${esc(context.company||'当前公司')} · ${esc(context.role||'当前岗位')}”的 JD、招聘进度和证据；普通问题不会写入职业档案。`:'例如：准备业务二面、开始数据归因专项训练，或者询问普通问题。普通对话不会写入职业档案。'}</p></div>`;
 thread.scrollTop=thread.scrollHeight;
}
function dashboardPanel(){const d=S.app.dashboard||{overall:0,dimensions:[],actions:[],risks:[]},next=d.actions[0];return `<section class="panel"><div class="row"><div><h2>岗位准备度</h2><strong class="overall-score">${d.overall??0}%</strong></div>${d.current_stage?`<span class="badge">当前：${esc(d.current_stage.name)}</span>`:'<span class="badge warn">尚未设置招聘环节</span>'}</div><div class="readiness-grid">${d.dimensions.map(x=>`<div class="readiness-card" title="${esc((d.explanations||[]).find(e=>e.key===x.key)?.reason||'')}"><div class="row"><strong>${esc(x.label)}</strong><span>${x.score}%</span></div><div class="meter"><span style="width:${x.score}%"></span></div><p class="small">${esc((d.explanations||[]).find(e=>e.key===x.key)?.reason||'')}</p></div>`).join('')}</div>${d.latest_training?`<p class="small">最近专项训练：${esc(d.latest_training.topic)} · ${d.latest_training.average_score}/100</p>`:''}${next?`<section class="panel next-action"><span class="pill">${esc(next.priority)}</span><h3>${esc(next.title)}</h3><p>${esc(next.reason)}</p><button data-action-target="${esc(next.target)}" data-action-key="${esc(next.key)}" class="primary">开始处理</button></section>`:''}${d.risks.length?`<h3>当前风险</h3>${list(d.risks)}`:''}</section>`;}
const stageTypeLabels={screening:'筛选',recruiter:'招聘沟通',business:'业务面试',hiring_manager:'直属主管',technical:'专业/技术面',case:'Case Study',assignment:'作业',panel:'交叉/小组面',director:'总监面',executive:'高管面',culture:'价值观面',offer:'Offer沟通',custom:'自定义'};
function recruitmentPanel(){const process=S.app.web.recruitment_process||{stages:[]},stages=process.stages||[];return `<section class="panel"><div class="row"><div><h2>招聘进程</h2><p class="minor">每个岗位独立维护，轮次名称和类型不写死。</p></div></div><div class="timeline">${stages.map(s=>`<article class="stage-card ${s.stage_id===process.current_stage_id?'current':''}"><span class="pill">${esc(stageTypeLabels[s.stage_type]||s.stage_type)}</span><h3>${esc(s.name)}</h3><p class="small">${esc(statusNames[s.status]||({planned:'计划中',scheduled:'已安排',waiting_result:'等待结果',passed:'已通过',failed:'未通过',cancelled:'已取消'})[s.status]||s.status)}${s.scheduled_at?'<br>'+esc(new Date(s.scheduled_at).toLocaleString()):s.scheduled_text?'<br>'+esc(s.scheduled_text):''}</p>${s.feedback?.length?`<details><summary>反馈与风险</summary>${list(s.feedback)}${list(s.risks)}</details>`:''}</article>`).join('')||'<p>还没有招聘环节，可以添加已知的下一轮。</p>'}</div><details><summary>添加或更新环节</summary><form id="stage-form"><label>环节名称<input name="name" required placeholder="例如：业务二面"></label><label>环节类型<select name="stage_type">${Object.entries(stageTypeLabels).map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></label><label>状态<select name="status"><option value="planned">计划中</option><option value="scheduled">已安排</option><option value="waiting_result">等待结果</option><option value="passed">已通过</option><option value="failed">未通过</option><option value="cancelled">已取消</option></select></label><label>面试官身份（选填）<input name="interviewer_role"></label><label>时间（选填）<input name="scheduled_at" type="datetime-local"></label><label class="label-row"><input name="make_current" type="checkbox" checked>设为当前环节</label><button class="primary" type="submit">保存环节</button></form></details></section>`;}
function pendingPanel(){const a=S.app;if(!a?.pending_validation_output)return '';const isResume=a.current_stage.includes('resume-tailor');if((isResume&&S.tab!=='resume')||(!isResume&&S.tab!=='prep'))return '';return `<section class="panel"><span class="badge warn">等待人工确认</span><p>这份候选尚未成为正式结果。请核对事实和提示；无法通过硬约束时，请修正源材料后重新生成。</p>${list(a.validation_issues)}${isResume?prose(resumeText(a.pending_validation_output.tailored_resume)):''}<div class="actions">${!isResume?'<button data-run="review_answers">审查现有答案</button>':''}<button id="approve">我已核对，确认当前候选</button></div></section>`;}
function resumeText(doc){if(!doc)return '';return [doc.candidate_name,doc.target_role,doc.professional_summary?.text,...(doc.sections||[]).flatMap(s=>[s.title,...s.entries.flatMap(e=>[e.heading,e.subheading,e.date_or_location,...e.bullets.map(b=>'• '+b.text)])])].filter(Boolean).join('\n\n');}
function render(){
 $('#mode').textContent=S.config?.provider==='mock'?'离线 Mock · 占位结果':'真实模型模式';
 $('#settings').innerHTML=S.config?`<p>快速模型：${esc(S.config.routes.fast)}<br>核心模型：${esc(S.config.routes.core)}<br>审查模型：${esc(S.config.routes.review)}</p><p>${esc(S.config.pricing_note)}</p>`:'';
 $('#jobs').innerHTML=S.jobs.map(j=>`<div class="job-row"><button data-job="${j.application_id}" class="job-select ${S.id===j.application_id&&S.page==='workspace'?'selected':''}">${esc(j.company||'待识别公司')}<br><span class="minor">${esc(j.role||'待识别岗位')}</span></button><button class="job-delete" data-delete-job="${j.application_id}" aria-label="删除岗位：${esc(j.company||'待识别公司')} / ${esc(j.role||'待识别岗位')}" title="删除岗位（可恢复）" ${S.locked||S.tasks.some(t=>t.application_id===j.application_id&&activeTask(t))?'disabled':''}>删除</button></div>`).join('')||'<p class="minor">尚无岗位</p>';
 $('#deleted-jobs > summary').textContent=`已删除岗位（${S.deleted.length}）`;
 $('#deleted-job-list').innerHTML=S.deleted.map(j=>`<div class="item"><span>${esc(j.company||'待识别公司')} / ${esc(j.role||'待识别岗位')}</span><button data-restore-job="${j.application_id}">恢复</button></div>`).join('')||'<p class="minor">暂无已删除岗位。</p>';
 root.querySelectorAll('aside [data-page]').forEach(b=>b.classList.toggle('selected',b.dataset.page===S.page));
 $('#job-header').hidden=S.page!=='workspace'||!S.app;
 if(S.app){$('#company-crumb').textContent=S.app.company||'待识别公司';$('#role-heading').textContent=S.app.role||'待识别岗位';$('#stage').textContent=statusNames[S.app.current_stage]||(S.app.current_stage.startsWith('waiting_for')?'待补充或确认':S.app.current_stage.startsWith('failed')?'执行失败':S.app.current_stage.startsWith('running')?'执行中':S.app.current_stage);}
 root.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('selected',b.dataset.tab===S.tab));progress();
 if(S.page==='materials')content.innerHTML='<h1>基础材料</h1><p class="minor">简历和作品集在这里统一管理，岗位使用独立快照。上传不会直接生成简历。</p>'+materialsPanel('resume')+materialsPanel('portfolio');
 else if(S.page==='evidence')renderEvidence();
 else if(S.page==='new')content.innerHTML='<h1>新建岗位</h1><p class="minor">公司和岗位可选填，之后可根据 JD 识别结果确认。</p><form id="new-form"><label>公司名称（选填）<input name="company" maxlength="200"></label><label>岗位名称（选填）<input name="role" maxlength="200"></label><div class="hint">创建后上传 JD。基础材料会单独关联，不影响已有岗位。</div><button class="primary" type="submit">创建岗位</button></form>';
 else if(S.page==='tasks')renderTasks();
 else if(!S.app)content.innerHTML='<h1>开始你的求职准备</h1><p>先上传基础材料，再新建岗位。</p>';
 else if(S.tab==='overview')renderDashboardOverview();else if(S.tab==='resume')renderResumeWithImpacts();else if(S.tab==='prep')renderInterviewProgress();else renderInterview();
 if(S.tab==='overview'&&S.page==='workspace'&&S.jdDrafts[S.id]&&$('#jd-profile-form')){for(const [name,value] of Object.entries(S.jdDrafts[S.id])){const el=$('#jd-profile-form').elements.namedItem(name);if(el)el.value=value;}$('#jd-editor').open=true;}
 if(busy())content.querySelectorAll('button:not([data-page]):not([data-tab]):not([data-session]),input[type=file]').forEach(b=>b.disabled=true);
 renderAgent();
}
function renderDashboardOverview(){renderOverview();content.insertAdjacentHTML('afterbegin',dashboardPanel());}
function renderInterviewProgress(){renderPrep();content.insertAdjacentHTML('afterbegin',recruitmentPanel());}
function renderResumeWithImpacts(){renderResume();const impacts=(S.app.web.material_impacts||[]).filter(i=>i.status==='pending');if(impacts.length)content.insertAdjacentHTML('afterbegin',`<section class="panel impact"><h2>基础材料已更新</h2>${impacts.map(i=>`<div class="item"><strong>${esc(i.message)}</strong><p>可能需要更新：${esc(i.affected_outputs.join('、'))}</p></div>`).join('')}<p class="small">重新关联当前基础材料后，系统会保留历史记录并使依赖结果失效。</p><button id="bind" class="primary">使用当前基础材料</button></section>`);}
function jdEditor(p){
 const field=(label,name,value,multi=false)=>`<label>${label}${multi?`<textarea name="${name}">${esc(value)}</textarea>`:`<input name="${name}" value="${esc(value)}">`}</label>`;
 const rows=(key,title)=>`<fieldset><legend>${title}</legend>${[...p[key],{item:'',priority:'',evidence:'',category:''}].map((row,i)=>`<div class="item">${field('内容（留空删除）',`${key}.${i}.item`,row.item)}${field('优先级',`${key}.${i}.priority`,row.priority)}${key==='requirements'?field('类别',`${key}.${i}.category`,row.category):''}${field('JD 原文依据',`${key}.${i}.evidence`,row.evidence)}</div>`).join('')}</fieldset>`;
 return `<details id="jd-editor"><summary>人工修正岗位分析</summary><p class="small">只按 JD 或已确认的信息修正。保存后，旧简历和面试准备结果需重新生成；原 JD 与历史面试会话保留。每次可在末行新增一条。</p><form id="jd-profile-form">${field('公司','company',p.company||'')}${field('岗位','role_title',p.role_title)}${field('岗位概述','role_summary',p.role_summary,true)}${field('资历要求','seniority',p.seniority||'')}${rows('responsibilities','职责')}${rows('requirements','要求')}${[['keywords','关键词'],['ambiguities','需要确认的信息'],['hidden_expectations','隐含要求']].map(([k,label])=>field(label+'（每行一条）',k,p[k].join('\n'),true)).join('')}<button type="submit">保存岗位修正</button></form></details>`;
}
function editedJD(form){const p=structuredClone(S.app.jd_profile);for(const k of ['company','role_title','role_summary','seniority'])p[k]=form[k].trim()||null;for(const k of ['keywords','ambiguities','hidden_expectations'])p[k]=form[k].split('\n').map(x=>x.trim()).filter(Boolean);for(const k of ['responsibilities','requirements'])p[k]=Array.from({length:p[k].length+1},(_,i)=>{const row={};for(const f of k==='requirements'?['item','priority','category','evidence']:['item','priority','evidence'])row[f]=(form[`${k}.${i}.${f}`]||'').trim();return row;}).filter(x=>x.item);return p;}
function renderOverview(){const a=S.app,p=a.jd_profile;const jd=S.materials.filter(m=>m.kind==='jd'&&m.application_id===S.id);content.innerHTML=`<h2>岗位 JD</h2><p class="minor">多张截图按上传顺序组合。可上传文本；扫描 PDF 请用截图。上传识别会调用模型。</p><input aria-label="上传岗位JD" type="file" data-upload="jd" multiple accept=".png,.jpg,.jpeg,.webp,.pdf,.txt,.md,.docx"><div>${jd.map(m=>`<div class="item"><span>${esc(m.name)} · ${esc(statusNames[m.status]||m.status)}${m.active?'':' · 已移出'}</span>${m.active?` <button data-deactivate="${m.material_id}">移出本岗位</button>`:''}${m.error?prose(m.error):''}</div>`).join('')}</div>${a.jd?`<details><summary>核对 JD 提取文字</summary>${prose(a.jd)}</details>`:''}<div class="actions"><button class="primary" data-run="analyze">分析岗位</button></div>${p?`<section class="panel"><h2>岗位分析结果</h2><form id="identity-form"><label>公司<input name="company" value="${esc(a.company||p.company||'')}" maxlength="200"></label><label>岗位<input name="role" value="${esc(a.role||p.role_title||'')}" maxlength="200"></label><button type="submit">确认公司和岗位</button></form>${jdEditor(p)}<h3>岗位概述</h3>${prose(p.role_summary)}<h3>职责</h3>${list(p.responsibilities?.map(r=>r.item))}<h3>要求与优先级</h3>${list(p.requirements?.map(r=>`${r.priority} · ${r.item}`))}<h3>关键词</h3>${list(p.keywords)}<h3>需要确认的信息</h3>${list(p.ambiguities)}<button data-tab="resume" class="primary">继续定制简历</button></section>`:''}`;}
function renderResume(){const a=S.app,m=a.web.material_snapshot;const draft=a.web.resume_draft;const text=S.drafts[S.id]??draft?.text??a.tailored_resume??'';content.innerHTML=`<h2>定制简历</h2><section class="panel"><h3>本岗位材料快照</h3><p>基础简历：${esc(m?.resume?.name||(a.base_resume?'旧版已保存简历':'尚未关联'))}<br>作品集：${esc(m?.portfolio?.map(p=>p.name).join('、')||(a.portfolio?'旧版已保存作品集':'未关联，可选'))}</p><p class="small">更新基础材料不会自动改变本岗位；重新关联会使依赖结果失效。旧面试会话保留原快照。</p><div class="actions"><button data-page="materials">管理基础材料</button><button id="bind">使用当前基础材料</button><button data-run="resume" class="primary">生成定制简历</button></div></section>${pendingPanel()}${text?`<section class="panel"><div class="row"><h3>简历正文</h3><span class="badge">${draft?'用户修改稿 · '+(draft.status==='stale'?'源材料已变化':'未重新审查'):'已保存生成结果'}</span></div><textarea id="resume-draft" aria-label="简历修改稿">${esc(text)}</textarea><p class="small">手动修改保存为独立草稿，不覆盖已审查生成稿。面试准备仍使用已生成的结构化结果。</p><div class="actions"><button id="save-draft">保存修改稿</button>${download('resume','下载生成简历')}${draft?download('draft','下载修改稿'):''}</div></section>`:''}<details><summary>修改说明与事实依据</summary>${list(a.resume_change_notes)}${(a.resume_claim_audit||[]).map(c=>`<div class="item"><strong>${esc(c.claim)}</strong><p>${esc(c.rationale)}</p><span class="small">依据：${esc(c.evidence_ids.join('、'))}</span></div>`).join('')}<h3>语义审查记录</h3>${resumeReviews(a.resume_semantic_review_history)}</details><div class="actions"><button data-tab="prep" class="primary">下一步：面试准备</button></div>`;}
function answerConfirmation(i,pending){
 const checked=S.app.answer_review_passed[i],confirmed=S.app.answer_confirmed[i];
 return `<div class="answer-confirmation"><span class="badge ${confirmed?'':'warn'}">${confirmed?'你已确认':'待你确认'}</span><p>${confirmed?'这道答案已确认。建议用自己的话练习表达。':'请核对这道答案是否符合你的真实经历；假设题请确认它表达的是你的处理思路。'}</p>${!checked?'<p class="small">请先点击“检查答案内容”，看看是否有夸大经历或需要补充的地方，再确认。</p>':''}${!confirmed?`<button data-confirm-answer="${i}" ${!checked?'disabled':''}>这道答案符合我的情况</button>`:''}</div>`;
}
function renderPrep(){
 const a=S.app,questions=a.interview_prep_profile?.questions||[],pending=a.current_stage==='waiting_for_human_review_interview-answer-coach';
 const profile=pending?a.pending_validation_output:a.answer_prep_profile;
 content.innerHTML=`<h2>面试准备</h2><div class="actions"><button data-run="questions">生成问题清单</button><button data-run="answers">生成准备答案</button>${profile?'<button data-run="review_answers">检查答案内容</button>':''}</div><section class="panel"><h3>可能的面试问题</h3>${questions.map(q=>`<div class="item"><span class="pill">${esc(q.priority)}</span><strong>${esc(q.question)}</strong><p class="small">${esc(q.why_likely)}</p><details><summary>可能追问</summary>${list(q.follow_ups)}</details></div>`).join('')||'<p>先生成问题清单；需要的岗位分析和简历会一起准备。</p>'}</section>${profile?`<section class="panel" id="prepared-answers"><div class="row"><h3>准备答案</h3>${download('answers','下载准备答案')}</div>${profile.answers.map((answer,i)=>`<div class="item answer-card" data-answer-index="${i}"><h3>${i+1}. ${esc(answer.question)}</h3><span class="pill">${esc(({experience:'真实经历',hypothetical:'假设处理思路',mixed:'经历与假设思路',auto:'按题意准备'})[answer.answer_mode]||'按题意准备')}</span><span class="pill">${a.answer_review_passed[i]?'内容检查已通过':'建议先检查内容'}</span>${prose(answer.recommended_answer||'还缺少一些个人信息，请先补充真实经历。')}<details><summary>回答思路与需要注意的地方</summary>${list(answer.answer_skeleton)}${list(answer.missing_information)}${list(answer.answer_risks)}<p class="small">参考材料：${esc(answer.evidence_to_use.join('、'))}</p></details><label>这道答案需要怎么调整？<textarea data-answer-feedback="${i}" placeholder="例如：这部分是我的设想，并非已经完成的经历，请修改表达。">${esc(S.answerFeedback[`${S.id}:${i}`]||'')}</textarea></label><button data-update-answer="${i}">修改这道答案</button>${answerConfirmation(i,pending)}</div>`).join('')}</section>`:''}<details><summary>面试参考与内容检查记录</summary>${a.interview_research_profile?list(a.interview_research_profile.findings?.map(f=>f.finding))+list(a.interview_research_profile.limitations):'<p>尚无参考内容。</p>'}${answerReviews(a.answer_reviews)}</details><div class="actions prep-next"><button data-tab="mock" class="primary" ${!questions.length?'disabled':''}>进入模拟面试</button></div>`;
}
function coach(feedback,questions){if(!feedback)return '';const levels={complete:'回答较完整',needs_more:'需要补充',unable:'暂时不会回答',not_evaluated:'本题未评价'};return `<section class="panel"><h2>本轮教练反馈</h2>${prose(feedback.overall_assessment)}${(feedback.questions||[]).map(q=>`<div class="item"><h3>第 ${q.main_index+1} 题 · ${levels[q.level]||q.level}</h3>${prose(questions[q.main_index])}${[['what_worked','值得保留'],['gaps','必要补充'],['optional_tips','可选建议'],['fact_notes','事实确认'],['reasoning_steps','处理思路']].map(([k,t])=>q[k]?.length?`<h4>${t}</h4>${list(q[k])}`:'').join('')}${q.supplement?'<h4>局部补充</h4>'+prose(q.supplement):''}${q.reference_answer?'<h4>参考回答（请核实个人事实）</h4>'+prose(q.reference_answer):''}${q.practice_follow_up?'<h4>建议重练</h4>'+prose(q.practice_follow_up):''}</div>`).join('')}<h3>下一步</h3>${list(feedback.next_actions)}</section>`;}
function followUpLabel(session,index){const d=(session.decisions||[]).find(d=>d.main_index===index&&d.action==='follow_up');return d?.follow_up_kind==='extension'?'延伸追问':d?.follow_up_kind==='supplement'?'补充追问':'追问';}
function renderInterview(){const s=S.session;content.innerHTML=`<div class="row"><h2>模拟面试</h2><button id="new-session">新开一轮</button></div><label>主问题数量（每题至多一次追问）<input id="question-count" type="number" min="1" max="10" value="3"></label><details ${!s?'open':''}><summary>历史会话（${S.sessions.length}）</summary>${S.sessions.map(h=>`<div class="item"><button data-session="${h.session_id}">${esc(new Date(h.created_at).toLocaleString())} · ${h.provider} · ${esc(statusNames[h.phase]||h.phase)} · ${h.turn_count} 次回答</button></div>`).join('')||'<p>还没有会话。生成问题清单后可以开始。</p>'}</details>${s?`<p class="small">会话 ${esc(s.session_id)} · ${s.provider==='mock'?'离线占位练习':'真实模型'} · 已提交记录自动保存</p>${s.turns.map(t=>`<div class="message"><strong>${t.kind==='follow_up'?followUpLabel(s,t.main_index):'主问题'} ${t.main_index+1}</strong>${prose(t.question)}</div><div class="message user">${prose(t.answer)}</div>`).join('')}${s.phase==='awaiting_answer'?`<section class="panel"><span class="badge">${s.current_kind==='follow_up'?followUpLabel(s,s.main_index):'主问题'} ${s.main_index+1} / ${s.questions.length}</span><div class="question">${esc(s.current_question)}</div><label>你的回答<textarea id="mock-answer" placeholder="按真实经历或明确的假设思路回答">${esc(S.answers[s.session_id]||'')}</textarea></label><div class="actions"><button id="submit-answer" class="primary">提交回答</button><button data-interview="skip">跳过</button><button id="pause">保存并暂停</button><button data-interview="finish">结束并生成反馈</button></div></section>`:s.phase!=='completed'?'<p>本次回答已保存，等待处理。失败后用上方任务续跑，或继续处理。</p><button data-interview="resume">继续处理</button>':''}${s.phase==='evaluate'?'<div class="actions"><button data-interview="move_on">保留回答，进入下一题</button><button data-interview="finish">结束并生成反馈</button></div>':''}${s.feedback_revision_pending?'<p class="danger">新反馈尚未完成，下方若有反馈仍为旧版。</p>':''}${coach(s.feedback,s.questions)}${s.phase==='completed'?'<button data-interview="feedback">只重新生成教练反馈</button>':''}<div class="actions">${download('session','下载整轮记录',s.session_id)} ${download('transcript','下载对话文本',s.session_id)} ${s.feedback?download('feedback','下载教练反馈',s.session_id):''}</div>`:''}`;}
async function runAction(action,payload={}){const task=await api(`/applications/${S.id}/tasks`,{method:'POST',body:{action,...payload}});S.tasks.unshift(task);clearNotice();await refresh();}
async function interview(operation,answer=''){const task=await api(`/applications/${S.id}/sessions/${S.session.session_id}/actions`,{method:'POST',body:{operation,answer}});S.tasks.unshift(task);if(operation==='answer')S.answers[S.session.session_id]='';await refresh();}
async function waitTask(id){for(;;){const t=await api('/tasks/'+id);const idx=S.tasks.findIndex(x=>x.task_id===id);if(idx>=0)S.tasks[idx]=t;else S.tasks.unshift(t);progress();if(!['queued','running'].includes(t.status)){if(t.status==='failed')throw new Error(t.error);return t;}await new Promise(r=>setTimeout(r,700));}}
async function guard(work){if(S.locked)return;clearNotice();S.operationScope=pageScope();S.locked=true;content.querySelectorAll('button:not([data-page]):not([data-tab]):not([data-session]),input[type=file]').forEach(el=>el.disabled=true);try{await work();}catch(e){notify(e.message,true);}finally{S.locked=false;S.operationScope=null;S.upload=null;await refresh().catch(e=>notify('连接后端失败：'+e.message,true));}}
root.addEventListener('keydown',e=>{
 if(e.target.id!=='agent-input'||e.key!=='Enter'||e.shiftKey||e.isComposing||e.keyCode===229)return;
 e.preventDefault();e.target.form?.requestSubmit();
});
root.addEventListener('submit',e=>{
 if(!['agent-form','stage-form'].includes(e.target.id))return;
 e.preventDefault();e.stopImmediatePropagation();
 guard(async()=>{
  if(e.target.id==='agent-form'){
   const input=$('#agent-input'),text=input.value.trim();if(!text)throw new Error('请输入消息。');
   const context=S.agentThread?.context||{},contextLabel=context.scope==='application'?`${context.company||'当前公司'} · ${context.role||'当前岗位'}`:'普通对话';
   S.agentPending={text,contextLabel,status:context.scope==='application'?'正在读取当前岗位进度与相关记忆…':'正在组织回答…'};input.value='';input.disabled=true;e.target.querySelector('button[type="submit"]').disabled=true;renderAgent();
   try{const result=await api('/agent/messages',{method:'POST',body:{text,application_id:S.id||null}});S.agentThread=result.thread;}
   finally{S.agentPending=null;input.disabled=false;e.target.querySelector('button[type="submit"]').disabled=false;renderAgent();}
  }else{
   const form=Object.fromEntries(new FormData(e.target));
   form.make_current=form.make_current==='on';form.focus_areas=[];form.feedback=[];form.risks=[];
   form.scheduled_at=form.scheduled_at||null;form.interviewer_role=form.interviewer_role||null;
   await api('/applications/'+S.id+'/recruitment-stages',{method:'POST',body:form});notify('招聘环节已保存，面试准备将使用新的轮次信息。');
  }
 });
},true);
root.addEventListener('click',e=>{
 const b=e.target.closest('button');if(!b)return;
 const special=b.id==='confirm-progress'||b.id==='cancel-progress'||b.dataset.evidenceId||b.dataset.actionTarget||b.dataset.agentPrompt;
 if(!special)return;e.preventDefault();e.stopImmediatePropagation();
 if(b.dataset.agentPrompt){$('#agent-input').value=b.dataset.agentPrompt;$('#agent-input').focus();return;}
 guard(async()=>{
  if(b.id==='confirm-progress'){
   const result=await api('/agent/proposals/'+b.dataset.proposalId+'/confirm',{method:'POST',body:{application_id:S.id||null}});S.agentThread=result.thread;
   if(result.result?.task)S.tasks.unshift(result.result.task);
   if(result.result?.session){S.session=result.result.session;localStorage.setItem('career-session-'+S.id,S.session.session_id);S.page='workspace';S.tab='mock';}
   notify('Agent 操作已执行，结果和运行记录已保存。');
  }else if(b.id==='cancel-progress'){
   S.agentThread=await api('/agent/proposals/'+b.dataset.proposalId+'/cancel',{method:'POST',body:{application_id:S.id||null}});notify('操作已取消，没有修改职业数据。');
  }else if(b.dataset.evidenceId){
   await api('/evidence/'+b.dataset.evidenceId,{method:'PATCH',body:{status:b.dataset.evidenceStatus}});notify('证据状态已更新。');
  }else if(b.dataset.actionTarget){
   S.tab=b.dataset.actionTarget;S.page='workspace';
   if(['analyze','resume','questions'].includes(b.dataset.actionKey))await runAction(b.dataset.actionKey);
  }
 });
},true);
root.addEventListener('input',e=>{
 if(e.target.id!=='evidence-search')return;const q=e.target.value.trim().toLowerCase();
 $('#evidence-list').innerHTML=(S.evidence.items||[]).filter(item=>!q||(item.text+' '+item.source).toLowerCase().includes(q)).map(evidenceCard).join('')||'<p>没有匹配证据。</p>';
});
root.addEventListener('input',e=>{if(e.target.dataset.answerFeedback!==undefined)S.answerFeedback[`${S.id}:${e.target.dataset.answerFeedback}`]=e.target.value;if(e.target.closest('#jd-profile-form'))S.jdDrafts[S.id]=Object.fromEntries(new FormData($('#jd-profile-form')));if(e.target.id==='resume-draft')S.drafts[S.id]=e.target.value;if(e.target.id==='mock-answer'&&S.session)S.answers[S.session.session_id]=e.target.value;});
root.addEventListener('change',e=>{if(!e.target.dataset.upload)return;const kind=e.target.dataset.upload,files=Array.from(e.target.files);guard(async()=>{for(const file of files){const form=new FormData();form.append('file',file);form.append('kind',kind);if(kind==='jd')form.append('application_id',S.id);S.upload={scope:S.operationScope||pageScope(),name:file.name};progress();const r=await api('/materials',{method:'POST',body:form});S.upload=null;S.tasks.unshift(r.task);await waitTask(r.task.task_id);}notify('材料已解析并保存。');});});
root.addEventListener('submit',e=>{e.preventDefault();guard(async()=>{const form=Object.fromEntries(new FormData(e.target));if(e.target.id==='new-form'){S.app=await api('/applications',{method:'POST',body:form});S.id=S.app.application_id;localStorage.setItem('career-current-job',S.id);S.session=null;S.page='workspace';S.tab='overview';clearNotice();}else if(e.target.id==='jd-profile-form'){await api('/applications/'+S.id+'/jd-profile',{method:'PUT',body:editedJD(form)});delete S.jdDrafts[S.id];notify('岗位修正已保存。后续生成将使用修正后的分析；已有简历和面试准备需重新生成。');}else if(e.target.id==='identity-form'){await api('/applications/'+S.id,{method:'PATCH',body:form});notify('公司和岗位已保存。');}});});
root.addEventListener('click',e=>{const b=e.target.closest('button');if(!b||b.disabled||(b.closest('form')&&b.type==='submit'))return;
 if(S.jdDrafts[S.id]&&(b.dataset.page||b.dataset.tab||b.dataset.job||b.dataset.run)){notify('请先保存岗位修正，再继续下一步。',true);return;}
 if(b.dataset.page){clearNotice();S.page=b.dataset.page;render();return;}if(b.dataset.tab){clearNotice();S.page='workspace';S.tab=b.dataset.tab;render();return;}
 guard(async()=>{
 if(b.dataset.deleteJob){const id=b.dataset.deleteJob;await api('/applications/'+id,{method:'DELETE'});delete S.drafts[id];delete S.jdDrafts[id];localStorage.removeItem('career-session-'+id);if(S.id===id){S.id=null;S.app=null;S.session=null;localStorage.removeItem('career-current-job');S.page='new';S.tab='overview';}clearNotice();}
 else if(b.dataset.restoreJob){await api('/deleted-applications/'+b.dataset.restoreJob+'/restore',{method:'POST'});clearNotice();}
 else if(b.dataset.job){clearNotice();S.id=b.dataset.job;localStorage.setItem('career-current-job',S.id);S.page='workspace';S.session=null;const job=S.jobs.find(j=>j.application_id===S.id);S.agentPending=null;S.agentThread={mode:'assistant',messages:[],pending_proposal:null,context:{scope:'application',company:job?.company,role:job?.role,readiness:0,risks:[]}};renderAgent();}
 else if(b.dataset.run)await runAction(b.dataset.run);
 else if(b.dataset.retry){const t=await api('/tasks/'+b.dataset.retry+'/retry',{method:'POST'});S.tasks.unshift(t);clearNotice();}
 else if(b.dataset.deactivate){await api('/materials/'+b.dataset.deactivate+'/deactivate',{method:'POST'});notify('已移出；已有岗位材料快照仍保留。');}
 else if(b.id==='bind'){await api('/applications/'+S.id+'/bind-materials',{method:'POST'});delete S.drafts[S.id];notify('本岗位已关联当前基础材料。');}
 else if(b.id==='save-draft'){await api('/applications/'+S.id+'/resume-draft',{method:'PUT',body:{text:$('#resume-draft').value}});notify('修改稿已保存，尚未重新审查。');}
 else if(b.id==='approve'){const r=await api('/applications/'+S.id+'/approve',{method:'POST'});notify(r.status==='semantic_review_required'?'还有答案未通过语义审查，请先点击审查现有答案。':r.status==='completed'?'已确认保存。':'未通过本地检查，请核对提示。',r.status!=='completed');}
 else if(b.dataset.confirmAnswer!==undefined){const i=Number(b.dataset.confirmAnswer),r=await api('/applications/'+S.id+'/confirm-answer',{method:'POST',body:{question_index:i,answer_hash:S.app.answer_hashes[i]}});notify(r.status==='review_required'?'请先检查答案内容，再确认这道答案。':['waiting_for_user','needs_changes'].includes(r.status)?'请补充真实信息或填写修改意见，再确认这道答案。':r.all_confirmed?'所有答案已确认，可以开始模拟面试。':'这道答案已确认。',['waiting_for_user','needs_changes'].includes(r.status));}
 else if(b.dataset.updateAnswer!==undefined){const i=Number(b.dataset.updateAnswer),feedback=$(`[data-answer-feedback="${i}"]`).value;await runAction('update_answer',{question_index:i,feedback});delete S.answerFeedback[`${S.id}:${i}`];}
 else if(b.id==='new-session'){const count=Number($('#question-count').value);S.session=await api(`/applications/${S.id}/sessions`,{method:'POST',body:{count}});localStorage.setItem('career-session-'+S.id,S.session.session_id);}
 else if(b.dataset.session){clearNotice();S.session=await api(`/applications/${S.id}/sessions/${b.dataset.session}`);localStorage.setItem('career-session-'+S.id,S.session.session_id);}
 else if(b.id==='submit-answer'){const answer=$('#mock-answer').value.trim();if(!answer)throw new Error('请先输入回答。');await interview('answer',answer);}
 else if(b.dataset.interview)await interview(b.dataset.interview);
 else if(b.id==='pause'){notify('已提交的回答已保存；未提交的草稿只保留在当前页面。可从历史会话继续。');S.page='workspace';S.tab='prep';}
 });
});
async function poll(){if(S.polling||S.locked)return;S.polling=true;try{const tasks=await api('/tasks');const changed=tasks.some(t=>{const old=S.tasks.find(x=>x.task_id===t.task_id);return old&&old.status!==t.status&&!['queued','running'].includes(t.status);});S.tasks=tasks;if(S.id&&tasks.some(t=>activeTask(t)&&t.application_id===S.id)){const aid=S.id,sid=S.session?.session_id;const [app,session]=await Promise.all([api('/applications/'+aid),sid?api(`/applications/${aid}/sessions/${sid}`):Promise.resolve(null)]);if(S.id===aid)S.app=app;if(session&&S.id===aid&&S.session?.session_id===sid)S.session=session;}tasks.forEach(t=>S.seen[t.task_id]=t.status);progress();if(S.page==='tasks')renderTasks();if(changed){clearNotice();await refresh();}}catch(e){notify('无法连接后端，已保存记录不会丢失。请检查服务终端。',true);}finally{S.polling=false;}}
(async()=>{try{S.config=await api('/config');S.page=S.id?'workspace':'new';await refresh();S.tasks.forEach(t=>S.seen[t.task_id]=t.status);setInterval(poll,2000);}catch(e){notify('启动失败：'+e.message,true);}})();
