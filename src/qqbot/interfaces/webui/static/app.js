const $ = (selector) => document.querySelector(selector);

let token = sessionStorage.getItem("kinna-bot-token") || "";
let state = null;
let original = {};

const glyphs = {
  connection: "⌁", conversation: "◌", personality: "✦", proactive: "◴",
  moderation: "◇", media: "▣", system: "⚙", memory: "◎", schedules: "◷",
  experiments: "⌬", guide: "?", dashboard: "▤", database: "▥", skills: "✧", wikiadmin: "▤",
  monitor: "◉", tools: "⌘", routing: "⇄",
};

let memoryView = { tab: "overview", scope: "", after: "", previous: [], query: "", status: "" };
let memoryRenderEpoch = 0;
let graphController = null;
let scheduleView = { tab: "jobs", overview: null, jobs: [], runs: [] };

async function request(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    throw new Error(body.error || `HTTP ${response.status}`);
  }
  return response;
}

async function api(path, options = {}) {
  return (await request(path, options)).json();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>\"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;",
  })[character]);
}

function toast(message, bad = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${bad ? " bad" : ""}`;
  clearTimeout(element._timer);
  element._timer = setTimeout(() => { element.className = "toast"; }, 2600);
}

function control(setting, value) {
  const current = value.value ?? "";
  const configured = value.configured && setting.secret
    ? '<span class="secret-hint">● 已配置，留空即不修改</span>' : "";
  let input;

  if (setting.kind === "boolean") {
    input = `<div class="toggle-row"><span class="muted">${current === "true" ? "已开启" : "已关闭"}</span><label class="switch"><input data-key="${setting.key}" type="checkbox" ${current === "true" ? "checked" : ""}><span></span></label></div>`;
  } else if (setting.kind === "select") {
    const options = setting.options.map((option) => `<option value="${escapeHtml(option)}" ${option === current ? "selected" : ""}>${escapeHtml(option || "默认")}</option>`).join("");
    input = `<select data-key="${setting.key}">${options}</select>`;
  } else if (setting.kind === "textarea") {
    input = `<textarea data-key="${setting.key}">${escapeHtml(current)}</textarea>`;
  } else {
    const attributes = setting.kind === "number"
      ? `type="number" ${setting.minimum !== null ? `min="${setting.minimum}"` : ""} ${setting.maximum !== null ? `max="${setting.maximum}"` : ""} step="any"`
      : `type="${setting.secret ? "password" : "text"}"`;
    const placeholder = setting.secret && value.configured ? "已配置，留空不修改" : "";
    input = `<input data-key="${setting.key}" ${attributes} value="${escapeHtml(current)}" placeholder="${placeholder}">`;
  }

  const search = escapeHtml(`${setting.label} ${setting.key} ${setting.help}`.toLowerCase());
  return `<div class="control" data-search="${search}"><label><span>${escapeHtml(setting.label)}</span><span class="key">${setting.key}</span></label>${input}<small>${configured}${configured && setting.help ? " · " : ""}${escapeHtml(setting.help)}</small></div>`;
}

function bindControls() {
  document.querySelectorAll('input[type="checkbox"][data-key]').forEach((element) => {
    element.onchange = () => {
      element.closest(".toggle-row").querySelector(".muted").textContent = element.checked ? "已开启" : "已关闭";
    };
  });
}

function formatTime(value) {
  if (!value) return "—";
  return new Date(Number(value) * 1000).toLocaleString("zh-CN", { hour12: false });
}

function statusBadge(status) {
  const labels = { active: "有效", candidate: "候选", accepted_as_report: "已接纳的说法", accepted: "已接纳", disputed: "有争议", withdrawn: "已撤回", observed: "已观察", unknown: "未确认", retired: "已停用", archived: "归档", expired: "过期", superseded: "已替代", succeeded: "成功", failed: "失败", running: "进行中", approved: "已审核", rejected: "已拒绝", completed: "完成", error: "失败", timeout: "超时" };
  return `<span class="data-badge status-${escapeHtml(status || "unknown")}">${escapeHtml(labels[status] || status || "未知")}</span>`;
}

function emptyState(title, detail) {
  return `<div class="empty-state"><span>○</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(detail)}</p></div>`;
}

async function memoryApi(path) {
  const separator = path.includes("?") ? "&" : "?";
  const scoped = memoryView.scope ? `${separator}scope=${encodeURIComponent(memoryView.scope)}` : "";
  const response = await api(`/api/memory/${path}${scoped}`);
  return response.data;
}

function memoryChrome(overview, body) {
  const tabs = [
    ["overview", "总览"], ["items", "长期记忆"], ["topics", "Topic"],
    ["people", "人物关系"], ["slang", "黑话"], ["persona_review", "人设建议"], ["extractions", "提炼记录"], ["graph", "关系图谱"],
  ];
  const scopes = [`<option value="">全部会话</option>`].concat(
    (overview.scopes || []).map((scope) => `<option value="${escapeHtml(scope)}" ${scope === memoryView.scope ? "selected" : ""}>${escapeHtml(scope)}</option>`)
  ).join("");
  return `<section class="memory-workspace">
    <div class="memory-toolbar">
      <div><h2>记忆中心</h2></div>
      <label class="scope-picker"><span>会话范围</span><select id="memory-scope">${scopes}</select></label>
    </div>
    <div class="memory-tabs" role="tablist">${tabs.map(([id, label]) => `<button role="tab" class="memory-tab ${memoryView.tab === id ? "active" : ""}" data-memory-tab="${id}">${label}</button>`).join("")}</div>
    <div class="memory-body">${body}</div>
  </section>`;
}

function renderOverview(data) {
  const memory = data.memories || {};
  const statuses = memory.by_status || {};
  const latest = memory.latest_extraction;
  return `<div class="metric-grid">
    <article class="metric-card"><span>长期记忆</span><strong>${memory.total || 0}</strong><small>${data.source === "ontology" ? `${statuses.accepted_as_report || 0} 条已接纳的说法` : `${statuses.active || 0} 条当前有效`}</small></article>
    <article class="metric-card"><span>Topic</span><strong>${memory.topics || 0}</strong><small>由长期事实关联形成</small></article>
    <article class="metric-card"><span>群友档案</span><strong>${data.people || 0}</strong><small>身份与关系状态</small></article>
    <article class="metric-card"><span>黑话释义</span><strong>${data.slang || 0}</strong><small>词义证据与起源分开记录</small></article>
  </div>
  <div class="overview-grid">
    <article class="data-panel"><div class="panel-title"><h3>记忆状态</h3></div>
      <div class="status-stack">${Object.keys(statuses).length ? Object.entries(statuses).map(([key, value]) => `<div><span>${statusBadge(key)}</span><b>${value}</b></div>`).join("") : `<p class="muted">还没有长期记忆。</p>`}</div>
    </article>
    <article class="data-panel"><div class="panel-title"><h3>最近一次提炼</h3></div>
      ${latest ? `<dl class="fact-grid"><div><dt>状态</dt><dd>${statusBadge(latest.status)}</dd></div><div><dt>消息范围</dt><dd>${latest.start_message_id}—${latest.end_message_id}</dd></div><div><dt>${data.source === "ontology" ? "写入 / 拒绝" : "采用 / 拒绝"}</dt><dd>${data.source === "ontology" ? latest.written : latest.accepted_count} / ${data.source === "ontology" ? latest.rejected : latest.rejected_count}</dd></div><div><dt>完成时间</dt><dd>${formatTime(latest.finished_at)}</dd></div></dl>${latest.error ? `<p class="inline-error">${escapeHtml(latest.error)}</p>` : ""}` : emptyState("尚未执行提炼", "达到消息间隔后会在后台自动运行。")}
    </article>
  </div>`;
}

function renderMemories(rows) {
  if (rows[0]?.ontology) return renderOntologyRecords(rows);
  if (!rows.length) return emptyState("还没有长期记忆", "新的提炼结果会在这里显示，并附带来源证据和修订记录。");
  return `<div class="list-head"><div><h3>长期记忆</h3><p>点击一条记录查看证据、Topic 和修订历史。</p></div><input id="memory-filter" type="search" placeholder="搜索记忆内容"></div>
  <div class="record-list" id="memory-records">${rows.map((row) => `<button class="record-card" data-memory-id="${row.id}" data-filter="${escapeHtml(row.content.toLowerCase())}"><div class="record-main"><div class="record-meta">${statusBadge(row.status)}<span>${escapeHtml(row.fact_type)}</span><span>${escapeHtml(row.user_id === "_group_" ? "群共享" : row.user_id)}</span></div><strong>${escapeHtml(row.content)}</strong></div><div class="record-side"><b>${Math.round(Number(row.confidence || 0) * 100)}%</b><small>${formatTime(row.updated_at)}</small></div></button>`).join("")}</div>`;
}

function renderTopics(rows) {
  if (rows[0]?.ontology) return renderOntologyRecords(rows);
  if (!rows.length) return emptyState("还没有长期 Topic", "带 Topic 的记忆提炼成功后会自动建立关联。");
  return `<div class="card-grid">${rows.map((row) => `<button class="topic-card" data-topic-id="${row.id}"><div class="topic-orbit"></div><span>${row.memory_count} 条记忆</span><h3>${escapeHtml(row.canonical_name)}</h3><p>${escapeHtml(row.summary || "暂无摘要")}</p><small>最近出现 ${formatTime(row.last_seen_at)}</small></button>`).join("")}</div>`;
}

function renderPeople(rows) {
  if (rows[0]?.ontology) return renderOntologyRecords(rows);
  if (!rows.length) return emptyState("还没有人物档案", "群友发言后会建立分群身份和关系记录。");
  return `<div class="people-grid">${rows.map((row) => `<button class="person-card" data-group-id="${escapeHtml(row.group_id)}" data-user-id="${escapeHtml(row.user_id)}"><span class="avatar-dot">${escapeHtml((row.nickname || row.user_id).slice(0, 1))}</span><div><h3>${escapeHtml(row.card || row.nickname || row.user_id)}</h3><p>${escapeHtml(row.stage_label || "普通关系")} · ${escapeHtml(row.group_id)}</p><small>${escapeHtml(row.relationship_note || "暂无关系变化")}</small></div><strong>${row.affection || 0}</strong></button>`).join("")}</div>`;
}

function renderSlang(rows) {
  if (rows[0]?.ontology) return renderOntologyRecords(rows);
  if (!rows.length) return emptyState("还没有黑话候选", "达到跨用户、跨日期和出现次数门槛后才会记录候选词。");
  return `<div class="slang-grid">${rows.map((row) => {
    const term = escapeHtml(row.term);
    const meaning = row.meaning || "";
    return `<article class="slang-card"><div><h3>${term}</h3>${statusBadge(row.status)}</div><p>${row.occurrences} 次出现 · ${row.seen_users.length} 位用户 · ${row.seen_days.length} 天</p><div class="confidence"><i style="width:${Math.round(Number(row.confidence || 0) * 100)}%"></i></div>${row.samples[0] ? `<blockquote>${escapeHtml(row.samples[0].text || "")}</blockquote>` : ""}
      <div class="review-row"><input class="review-meaning" data-term="${term}" value="${escapeHtml(meaning)}" placeholder="这个词在本群是什么意思？"><div class="review-actions"><button data-review="approve" data-term="${term}">通过</button><button data-review="reject" data-term="${term}">拒绝</button><button data-review="reset" data-term="${term}">重置</button></div></div>
      ${meaning ? "" : `<small class="review-hint">没有释义的词不会被注入——先写下它的意思，再点通过。</small>`}
      ${row.meaning_source === "human" ? `<small class="review-hint">释义是你写的，后续自动推断不会覆盖它。清空输入框保存即可交还给自动推断。</small>` : ""}
    </article>`;
  }).join("")}</div>`;
}

function renderPersonaQueue(data) {
  const pending = data.pending || [];
  const decided = data.decided || [];
  if (!pending.length && !decided.length) {
    return emptyState("还没有风格演进记录", "有足够的新对话后，系统会挑选合格的真实接话示例；基本人设保持不变。");
  }
  const card = (row, live) => `<article class="proposal-card"><div class="record-meta">${statusBadge(row.status === "pending" ? "candidate" : row.status)}<span>${row.kind === "example" ? "真实对话示例" : "表达建议"}</span><span>${escapeHtml(row.scope)}</span><span>${escapeHtml(formatTime(row.created_at))}</span></div><h3 style="white-space:pre-line">${escapeHtml(row.suggestion)}</h3>${row.rationale ? `<p>${escapeHtml(row.rationale)}</p>` : ""}${row.note ? `<small>批注：${escapeHtml(row.note)}</small>` : ""}${live ? `<div class="review-actions"><button data-proposal="${row.id}" data-status="accepted" data-kind="${row.kind || "guidance"}">${row.kind === "example" ? "采用为示例" : "认可"}</button><button data-proposal="${row.id}" data-status="rejected" data-kind="${row.kind || "guidance"}">不采纳</button></div>` : ""}</article>`;
  return `<div class="proposal-list"><p class="proposal-note">基本人设不自动改。启用自动演进时，只轮换下方真实对话示例；每次修改留有历史备份。旧版待审建议仍可手动处理。</p>${pending.map((row) => card(row, true)).join("")}${decided.map((row) => card(row, false)).join("")}</div>`;
}

async function reviewApi(path, body) {
  const response = await api(`/api/review/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return response;
}

/** Save a definition, a decision, or both, then re-render.
 *
 *  The definition is written first so that an approval takes effect together
 *  with it — approving a term with no meaning on file leaves it out of the
 *  prompt, and doing the two writes in this order avoids a refresh that shows
 *  an approved term the bot still cannot use. */
async function submitReview(term, action, meaning) {
  try {
    const text = String(meaning || "").trim();
    // An emptied box removes the definition; reset goes further and releases
    // the override entirely, so automatic inference can take the term back
    // instead of being frozen by a stale correction.
    await reviewApi("meaning", {
      term,
      meaning: action === "reset" ? null : text,
    });
    if (action) await reviewApi("decision", { term, action });
    toast(action ? "已记录审查决定" : "已保存释义");
    await renderMemory("slang");
  } catch (error) {
    toast(error.message || "写入失败", true);
  }
}

function renderExtractions(rows) {
  if (!rows.length) return emptyState("还没有提炼记录", "下一次记忆提炼会记录消息边界、结果和错误原因。");
  if (rows[0]?.source === "ontology") return `<div class="run-list">${rows.map((row) => `<article class="run-card"><div class="run-marker ${escapeHtml(row.status)}"></div><div><div class="record-meta">${statusBadge(row.status)}<span>${escapeHtml(row.strategy_version)}</span><span>${escapeHtml(row.scope)}</span></div><h3>消息 ${row.start_message_id}—${row.end_message_id}</h3><p>读取 ${row.message_count} 条 · 提议 ${row.proposed} 条 · 写入 ${row.written} 条 · 拒绝 ${row.rejected} 条</p>${row.error ? `<small class="inline-error">${escapeHtml(row.error)}</small>` : `<small>${formatTime(row.finished_at || row.started_at)}</small>`}</div></article>`).join("")}</div>`;
  return `<div class="run-list">${rows.map((row) => `<article class="run-card"><div class="run-marker ${escapeHtml(row.status)}"></div><div><div class="record-meta">${statusBadge(row.status)}<span>${escapeHtml(row.strategy_version)}</span></div><h3>消息 ${row.start_message_id}—${row.end_message_id}</h3><p>处理 ${row.message_count} 条，采用 ${row.accepted_count} 条，拒绝 ${row.rejected_count} 条。</p>${row.error ? `<small class="inline-error">${escapeHtml(row.error)}</small>` : `<small>${formatTime(row.finished_at || row.started_at)}</small>`}</div></article>`).join("")}</div>`;
}

function ontologyButton(row, label = row.label) {
  return `<button class="linked-memory" data-ontology-id="${escapeHtml(row.ref_id)}" data-ontology-scope="${escapeHtml(row.scope)}">${statusBadge(row.status)}<span>${escapeHtml(String(label))}</span></button>`;
}

function bindOntologyDetails(root = document) {
  root.querySelectorAll("[data-ontology-id]").forEach((button) => {
    button.onclick = () => openOntology({scope: button.dataset.ontologyScope, ref_id: button.dataset.ontologyId});
  });
}

function renderOntologyRecords(rows) {
  return `<div class="record-list">${rows.map((row) => `<button class="record-card" data-ontology-id="${escapeHtml(row.ref_id)}" data-ontology-scope="${escapeHtml(row.scope)}"><div class="record-main"><div class="record-meta">${statusBadge(row.status)}<span>${escapeHtml(row.scope)}</span>${row.metadata.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div><strong>${escapeHtml(String(row.label))}</strong>${row.summary ? `<p>${escapeHtml(row.summary)}</p>` : ""}</div><div class="record-side"><small>${formatTime(row.updated_at_ms / 1000)}</small></div></button>`).join("")}</div>`;
}

async function showDetail(title, content) {
  $("#detail-title").textContent = title;
  $("#detail-content").innerHTML = content;
  $("#detail-dialog").showModal();
}

async function openMemory(id) {
  const row = (await api(`/api/memory/items/${id}`)).data;
  const evidence = row.evidence || [], revisions = row.revisions || [], topics = row.topics || [];
  showDetail("长期记忆", `<div class="detail-lead">${statusBadge(row.status)}<h3>${escapeHtml(row.content)}</h3><p>主体 ${escapeHtml(row.user_id)} · 置信度 ${Math.round(Number(row.confidence) * 100)}% · 重要度 ${row.importance}</p></div>
    <section class="detail-section"><h4>Topic</h4>${topics.length ? `<div class="chip-row">${topics.map((item) => `<span>${escapeHtml(item.canonical_name)}</span>`).join("")}</div>` : `<p class="muted">未关联 Topic</p>`}</section>
    <section class="detail-section"><h4>来源证据</h4>${evidence.length ? evidence.map((item) => `<article class="evidence"><b>${escapeHtml(item.user_id || "未知用户")}</b><p>${escapeHtml(item.excerpt)}</p><small>${escapeHtml(item.event_id)} · ${formatTime(item.observed_at)}</small></article>`).join("") : `<p class="muted">旧记忆没有证据记录。</p>`}</section>
    <section class="detail-section"><h4>修订历史</h4>${revisions.length ? revisions.map((item) => `<div class="revision"><span>${escapeHtml(item.action)}</span><p>${escapeHtml(item.new_content || item.old_content || item.reason)}</p><small>${formatTime(item.created_at)}</small></div>`).join("") : `<p class="muted">暂无修订记录。</p>`}</section>`);
}

async function openOntology(node) {
  try {
    const row = (await api(`/api/memory/ontology-detail?scope=${encodeURIComponent(node.scope)}&id=${encodeURIComponent(node.ref_id)}`)).data;
    const item = row.node;
    const labels = {candidate: "候选说法", accepted_as_report: "已接纳的说法", disputed: "有争议", superseded: "已修订", expired: "已过期", withdrawn: "已撤回"};
    const state = row.display?.status || item.epistemic_status || item.status || "observed";
    const label = row.display?.label ?? item.typed_value?.value ?? item.name ?? item.definition ?? item.account_ref ?? node.label ?? item.type;
    showDetail("图谱记录", `<div class="detail-lead"><span class="status-badge">${escapeHtml(labels[state] || state)}</span><h3>${escapeHtml(String(label))}</h3><p>${escapeHtml(row.interpretation)}</p></div>
      <section class="detail-section"><h4>来源证据</h4>${(row.evidence || []).map((e) => `<article class="evidence"><p>${escapeHtml(e.excerpt)}</p><small>${escapeHtml(e.source_id)} · ${escapeHtml(e.role)}</small></article>`).join("") || '<p class="muted">此记录没有可展示的原文证据。</p>'}</section>
      ${item.type === "SlangSense" ? `<section class="detail-section"><h4>词义与来源</h4><p>${escapeHtml(item.definition)}</p><small>首次观察 ${formatTime(item.first_observed_at / 1000)}；首次观察不等于起源。</small>${(row.origin_evidence || []).map((e) => `<article class="evidence"><p>${escapeHtml(e.excerpt)}</p></article>`).join("") || '<p class="muted">尚无证明起源的证据。</p>'}</section>` : ""}
      ${item.type === "SocialState" ? `<section class="detail-section"><h4>互动状态</h4><p>好感度 ${Number(item.affection)} · ${escapeHtml(item.relationship_stage)}</p><div class="chip-row">${Object.entries(item.emotion).map(([key, value]) => `<span>${escapeHtml(key)} ${Number(value)}</span>`).join("")}</div></section>` : ""}
      <section class="detail-section"><h4>关联记录</h4>${(row.related || []).map((record) => ontologyButton(record, `${record.relation} · ${record.label}`)).join("") || '<p class="muted">没有关联记录。</p>'}</section>
      <section class="detail-section"><h4>修订历史</h4>${(row.revisions || []).map((revision) => `<div class="revision"><span>版本 ${revision.revision}</span><p>${escapeHtml(revision.reason)}</p><small>${statusBadge(revision.node.epistemic_status || revision.node.status || "observed")} · ${formatTime(revision.node.updated_at / 1000)}</small></div>`).join("")}</section>
      <section class="detail-section"><small>版本 ${row.revision} · ${escapeHtml(item.type)} · ${escapeHtml(item.id)}</small></section>`);
    bindOntologyDetails($("#detail-content"));
  } catch (error) {
    showDetail("图谱记录", `<p class="inline-error">${escapeHtml(error.message || "读取失败，请重新加载图谱。")}</p>`);
  }
}

async function openTopic(id) {
  const row = (await api(`/api/memory/topics/${id}`)).data;
  showDetail(row.canonical_name, `<div class="detail-lead"><h3>${escapeHtml(row.summary || row.canonical_name)}</h3><p>${row.memory_count || (row.memories || []).length} 条关联记忆 · 最近出现 ${formatTime(row.last_seen_at)}</p></div><section class="detail-section"><h4>关联记忆</h4>${(row.memories || []).map((item) => `<button class="linked-memory" data-memory-id="${item.id}">${statusBadge(item.status)}<span>${escapeHtml(item.content)}</span></button>`).join("") || `<p class="muted">暂无关联记忆。</p>`}</section>`);
  document.querySelectorAll("#detail-content [data-memory-id]").forEach((button) => { button.onclick = () => openMemory(button.dataset.memoryId); });
}

async function openPerson(groupId, userId) {
  const row = (await api(`/api/memory/people/${encodeURIComponent(groupId)}/${encodeURIComponent(userId)}`)).data;
  showDetail(row.card || row.nickname || row.user_id, `<div class="detail-lead"><span class="avatar-dot large">${escapeHtml((row.nickname || row.user_id).slice(0, 1))}</span><h3>${escapeHtml(row.stage_label)}</h3><p>${escapeHtml(row.relationship_note)}</p></div><section class="detail-section"><h4>关系变化</h4>${(row.history || []).map((item) => `<div class="revision"><span>${item.delta > 0 ? "+" : ""}${item.delta}</span><p>${escapeHtml(item.reason)}</p><small>${formatTime(item.created_at)} · ${escapeHtml(item.status)}</small></div>`).join("") || `<p class="muted">暂无变化记录。</p>`}</section><section class="detail-section"><h4>人物记忆</h4>${(row.memories || []).map((item) => `<button class="linked-memory" data-memory-id="${item.id}">${statusBadge(item.status)}<span>${escapeHtml(item.content)}</span></button>`).join("") || `<p class="muted">暂无人物记忆。</p>`}</section>`);
  document.querySelectorAll("#detail-content [data-memory-id]").forEach((button) => { button.onclick = () => openMemory(button.dataset.memoryId); });
}

async function renderMemory(tab = memoryView.tab) {
  const epoch = ++memoryRenderEpoch;
  if (tab !== memoryView.tab) { memoryView.after = ""; memoryView.previous = []; }
  memoryView.tab = tab;
  if (graphController) { graphController.destroy(); graphController = null; }
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "memory"));
  $(".search").classList.add("hidden");
  $("#save").classList.add("hidden");
  $("#content").innerHTML = `<section class="memory-workspace loading-state">正在读取记忆数据…</section>`;
  history.replaceState(null, "", "#memory");
  try {
    const overview = await memoryApi("overview");
    const page = `limit=200${memoryView.after ? `&after=${encodeURIComponent(memoryView.after)}` : ""}`;
    let payload = null;
    if (tab === "items") payload = await memoryApi(`items?${page}&q=${encodeURIComponent(memoryView.query)}&status=${encodeURIComponent(memoryView.status)}`);
    if (tab === "topics") payload = await memoryApi(`topics?${page}`);
    if (tab === "people") payload = await memoryApi(`people?${page}`);
    if (tab === "slang") payload = await memoryApi(`slang?${page}`);
    if (tab === "persona_review") payload = (await api(`/api/review/persona${memoryView.scope ? `?scope=${encodeURIComponent(memoryView.scope)}` : ""}`)).data;
    if (tab === "extractions") payload = await memoryApi("extractions?limit=200");
    if (tab === "graph") payload = await memoryApi("graph?limit=120");
    const body = tab === "overview" ? renderOverview(overview)
      : tab === "items" ? renderMemories(payload)
      : tab === "topics" ? renderTopics(payload)
      : tab === "people" ? renderPeople(payload)
      : tab === "slang" ? renderSlang(payload)
      : tab === "persona_review" ? renderPersonaQueue(payload)
      : tab === "extractions" ? renderExtractions(payload)
      : overview.source === "ontology" && !memoryView.scope ? emptyState("选择一个群查看图谱", "右上角选择会话范围；不同群的图谱分别展示。")
      : `<div class="graph-shell"><div class="graph-note"><span class="graph-note-avatar"><img src="/kinna-avatar.png" alt=""></span><div class="graph-note-text"><b>关系图谱</b><span>滚轮缩放 · 拖动画布 · 拖拽节点 · 点击查看详情</span></div></div><canvas id="memory-graph" aria-label="记忆关系图谱"></canvas><div class="graph-legend"><span class="legend-topic">Topic</span><span class="legend-memory">记忆</span><span class="legend-person">群友</span><span class="legend-bot">Bot</span></div></div>`;
    if (epoch !== memoryRenderEpoch) return;
    const paged = overview.source === "ontology" && ["items", "topics", "people", "slang"].includes(tab);
    const pagination = paged ? `<div class="review-actions"><button id="memory-previous" ${memoryView.previous.length ? "" : "disabled"}>上一页</button><span>本页 ${payload.length} 条</span><button id="memory-next" ${payload.length >= 200 ? "" : "disabled"}>下一页</button></div>` : "";
    const filters = overview.source === "ontology" && tab === "items" ? `<form id="ontology-search" class="memory-toolbar"><input id="ontology-query" type="search" placeholder="搜索记忆内容" value="${escapeHtml(memoryView.query)}"><select id="ontology-status" aria-label="记忆状态">${["", "candidate", "accepted_as_report", "disputed", "superseded", "expired", "withdrawn"].map((state) => `<option value="${state}" ${state === memoryView.status ? "selected" : ""}>${({candidate: "候选", accepted_as_report: "已接纳的说法", disputed: "有争议", superseded: "已替代", expired: "过期", withdrawn: "已撤回"})[state] || "全部状态"}</option>`).join("")}</select><button type="submit">搜索</button></form>` : "";
    $("#content").innerHTML = memoryChrome(overview, filters + body + pagination);
    if ($("#ontology-search")) $("#ontology-search").onsubmit = (event) => {
      event.preventDefault(); memoryView.query = $("#ontology-query").value; memoryView.status = $("#ontology-status").value;
      memoryView.after = ""; memoryView.previous = []; renderMemory();
    };
    $("#memory-scope").onchange = (event) => { memoryView.scope = event.target.value; memoryView.after = ""; memoryView.previous = []; renderMemory(); };
    if (paged) {
      $("#memory-next").onclick = () => { memoryView.previous.push(memoryView.after); memoryView.after = payload.at(-1).cursor; renderMemory(); };
      $("#memory-previous").onclick = () => { memoryView.after = memoryView.previous.pop() || ""; renderMemory(); };
    }
    bindOntologyDetails();
    document.querySelectorAll("[data-memory-tab]").forEach((button) => { button.onclick = () => renderMemory(button.dataset.memoryTab); });
    document.querySelectorAll("[data-memory-id]").forEach((button) => { button.onclick = () => openMemory(button.dataset.memoryId); });
    document.querySelectorAll("[data-topic-id]").forEach((button) => { button.onclick = () => openTopic(button.dataset.topicId); });
    document.querySelectorAll("[data-user-id]").forEach((button) => { button.onclick = () => openPerson(button.dataset.groupId, button.dataset.userId); });
    document.querySelectorAll("[data-review]").forEach((button) => {
      button.onclick = () => {
        const card = button.closest(".slang-card");
        const input = card && card.querySelector(".review-meaning");
        submitReview(button.dataset.term, button.dataset.review, input ? input.value : "");
      };
    });
    document.querySelectorAll(".review-meaning").forEach((input) => {
      input.onchange = () => submitReview(input.dataset.term, "", input.value);
    });
    document.querySelectorAll("[data-proposal]").forEach((button) => {
      button.onclick = async () => {
        try {
          await api(`/api/review/persona/${button.dataset.proposal}`, {
            method: "POST",
            body: JSON.stringify({ status: button.dataset.status }),
          });
          toast(button.dataset.status === "accepted" ? (button.dataset.kind === "example" ? "已写入风格示例" : "已认可，指导建议仍需手动修改") : "已标记为不采纳");
          await renderMemory("persona_review");
        } catch (error) {
          toast(error.message || "写入失败", true);
        }
      };
    });
    const filter = $("#memory-filter");
    if (filter) filter.oninput = () => document.querySelectorAll("#memory-records .record-card").forEach((item) => item.classList.toggle("hidden", !item.dataset.filter.includes(filter.value.trim().toLowerCase())));
    if (tab === "graph" && $("#memory-graph")) graphController = createMemoryGraph($("#memory-graph"), payload);
  } catch (error) {
    if (epoch !== memoryRenderEpoch) return;
    $("#content").innerHTML = `<section class="memory-workspace">${emptyState("记忆中心加载失败", error.message || "请稍后重试")}</section>`;
  }
}

function createMemoryGraph(canvas, data) {
  const context = canvas.getContext("2d");
  const signal = new AbortController();
  const colors = { bot: "#1769e0", person: "#33a584", topic: "#7b61c9", memory: "#e09635" };
  const radii = { bot: 13, person: 8, topic: 10, memory: 6 };
  const nodes = (data.nodes || []).map((item, index) => {
    let seed = 0;
    for (const character of item.id) seed = (seed * 31 + character.charCodeAt(0)) >>> 0;
    const angle = ((seed % 360) / 180) * Math.PI;
    const distance = item.kind === "bot" ? 0 : item.kind === "person" ? 120 : item.kind === "topic" ? 210 : 280;
    return { ...item, x: Math.cos(angle) * distance, y: Math.sin(angle) * distance, vx: 0, vy: 0, index };
  });
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const links = (data.links || []).map((item) => ({ ...item, source: byId.get(item.source), target: byId.get(item.target) })).filter((item) => item.source && item.target);
  let width = 1, height = 1, ratio = window.devicePixelRatio || 1;
  let camera = { x: 0, y: 0, scale: 1 };
  let hovered = null, dragged = null, moved = false, last = null, frame = 0, settled = false;

  function resize() {
    const rect = canvas.getBoundingClientRect();
    width = Math.max(1, rect.width); height = Math.max(1, rect.height);
    ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  function world(event) {
    const rect = canvas.getBoundingClientRect();
    return { x: (event.clientX - rect.left - width / 2 - camera.x) / camera.scale, y: (event.clientY - rect.top - height / 2 - camera.y) / camera.scale };
  }

  function hit(point) {
    let found = null, best = Infinity;
    for (const node of nodes) {
      const distance = Math.hypot(node.x - point.x, node.y - point.y);
      const radius = (radii[node.kind] || 6) + 7 / camera.scale;
      if (distance < radius && distance < best) { found = node; best = distance; }
    }
    return found;
  }

  function simulate() {
    if (!settled) {
      for (let i = 0; i < nodes.length; i += 1) {
        const left = nodes[i];
        for (let j = i + 1; j < nodes.length; j += 1) {
          const right = nodes[j];
          let dx = right.x - left.x, dy = right.y - left.y;
          const squared = Math.max(80, dx * dx + dy * dy);
          const force = Math.min(1.8, 850 / squared);
          const distance = Math.sqrt(squared); dx /= distance; dy /= distance;
          left.vx -= dx * force; left.vy -= dy * force;
          right.vx += dx * force; right.vy += dy * force;
        }
      }
      for (const link of links) {
        const dx = link.target.x - link.source.x, dy = link.target.y - link.source.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const target = link.kind === "relationship" ? 135 : link.kind === "topic" ? 80 : 100;
        const force = (distance - target) * 0.0025;
        link.source.vx += (dx / distance) * force; link.source.vy += (dy / distance) * force;
        link.target.vx -= (dx / distance) * force; link.target.vy -= (dy / distance) * force;
      }
      let motion = 0;
      for (const node of nodes) {
        if (node === dragged) continue;
        node.vx += -node.x * 0.00035; node.vy += -node.y * 0.00035;
        node.vx *= 0.88; node.vy *= 0.88;
        node.x += node.vx; node.y += node.vy;
        motion += Math.abs(node.vx) + Math.abs(node.vy);
      }
      settled = motion < 0.018 && !dragged;
    }
    draw();
    frame = requestAnimationFrame(simulate);
  }

  function draw() {
    context.save();
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.translate(width / 2 + camera.x, height / 2 + camera.y);
    context.scale(camera.scale, camera.scale);
    const neighbors = new Set();
    if (hovered) for (const link of links) {
      if (link.source === hovered) neighbors.add(link.target);
      if (link.target === hovered) neighbors.add(link.source);
    }
    for (const link of links) {
      const active = hovered && (link.source === hovered || link.target === hovered);
      context.beginPath(); context.moveTo(link.source.x, link.source.y); context.lineTo(link.target.x, link.target.y);
      context.strokeStyle = active ? "rgba(23,105,224,.75)" : "rgba(102,130,166,.20)";
      context.lineWidth = (active ? 1.8 : 0.8) / camera.scale; context.stroke();
    }
    for (const node of nodes) {
      const radius = radii[node.kind] || 6;
      const dimmed = hovered && node !== hovered && !neighbors.has(node);
      context.globalAlpha = dimmed ? 0.2 : 1;
      context.beginPath(); context.arc(node.x, node.y, radius, 0, Math.PI * 2);
      context.fillStyle = colors[node.kind] || "#718399"; context.fill();
      context.lineWidth = (node === hovered ? 3 : 1.5) / camera.scale;
      context.strokeStyle = node === hovered ? "#fff" : "rgba(255,255,255,.8)"; context.stroke();
      if (node === hovered || node.kind === "bot" || (node.kind === "topic" && camera.scale > .65)) {
        context.font = `${node === hovered ? 600 : 500} ${12 / camera.scale}px Inter, sans-serif`;
        context.fillStyle = "#18324f"; context.textAlign = "center";
        const label = node.label.length > 22 ? `${node.label.slice(0, 22)}…` : node.label;
        context.fillText(label, node.x, node.y + radius + 16 / camera.scale);
      }
    }
    context.globalAlpha = 1; context.restore();
  }

  canvas.addEventListener("pointerdown", (event) => {
    last = { x: event.clientX, y: event.clientY }; moved = false;
    dragged = hit(world(event));
    canvas.setPointerCapture(event.pointerId);
    settled = false;
  }, { signal: signal.signal });
  canvas.addEventListener("pointermove", (event) => {
    const point = world(event);
    if (last) {
      const dx = event.clientX - last.x, dy = event.clientY - last.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      if (dragged) { dragged.x = point.x; dragged.y = point.y; dragged.vx = 0; dragged.vy = 0; }
      else { camera.x += dx; camera.y += dy; }
      last = { x: event.clientX, y: event.clientY };
    } else {
      hovered = hit(point); canvas.style.cursor = hovered ? "pointer" : "grab"; draw();
    }
  }, { signal: signal.signal });
  canvas.addEventListener("pointerup", (event) => {
    if (!moved && dragged) {
      if (dragged.ontology) openOntology(dragged);
      else {
        if (dragged.kind === "memory") openMemory(dragged.ref_id);
        if (dragged.kind === "topic") openTopic(dragged.ref_id);
        if (dragged.kind === "person") openPerson(dragged.group_id, dragged.ref_id);
      }
    }
    dragged = null; last = null; canvas.releasePointerCapture(event.pointerId);
  }, { signal: signal.signal });
  canvas.addEventListener("pointerleave", () => { if (!last) { hovered = null; draw(); } }, { signal: signal.signal });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const before = world(event), factor = Math.exp(-event.deltaY * 0.001);
    camera.scale = Math.max(0.35, Math.min(3.5, camera.scale * factor));
    const after = world(event);
    camera.x += (after.x - before.x) * camera.scale;
    camera.y += (after.y - before.y) * camera.scale;
    draw();
  }, { passive: false, signal: signal.signal });
  const observer = new ResizeObserver(resize); observer.observe(canvas);
  resize(); simulate();
  return { destroy() { signal.abort(); observer.disconnect(); cancelAnimationFrame(frame); } };
}

async function renderGuide() {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.target === "guide");
  });
  $(".search").classList.add("hidden");
  $("#save").classList.add("hidden");
  $("#content").innerHTML = '<section class="guide markdown-shell"><p class="guide-loading">正在读取指南…</p></section>';
  history.replaceState(null, "", "#guide");

  try {
    const html = await (await request("/api/guide")).text();
    $("#content").innerHTML = `<article class="guide markdown-body">${html}</article>`;
  } catch (error) {
    $("#content").innerHTML = `<section class="guide markdown-shell"><p class="error">${escapeHtml(error.message || "指南加载失败")}</p></section>`;
  }
}

function scheduleLabel(job) {
  if (job.schedule_kind === "at") return `单次 · ${job.schedule_value}`;
  if (job.schedule_kind === "every") return `每 ${Math.round(Number(job.schedule_value) / 60)} 分钟`;
  return `Cron · ${job.schedule_value}`;
}

function scheduleStatus(job) {
  if (job.expired) return statusBadge("expired");
  return job.enabled ? statusBadge("active") : `<span class="data-badge">已停用</span>`;
}

function scheduleChrome(body) {
  const tabs = [["jobs", "任务列表"], ["timeline", "时间线"], ["runs", "运行记录"], ["templates", "任务模板"]];
  const overview = scheduleView.overview || {};
  return `<section class="schedule-workspace">
    <div class="schedule-toolbar"><div><h2>任务中心</h2></div><button id="schedule-create" class="button primary" type="button">新建任务</button></div>
    <div class="metric-grid schedule-metrics">
      <article class="metric-card"><span>已启用</span><strong>${overview.enabled || 0}</strong><small>当前有效任务</small></article>
      <article class="metric-card"><span>未来 24 小时</span><strong>${overview.next_24h || 0}</strong><small>${escapeHtml(overview.timezone || "")}</small></article>
      <article class="metric-card"><span>正在运行</span><strong>${overview.running || 0}</strong><small>已预留的执行</small></article>
      <article class="metric-card"><span>近 7 日失败</span><strong>${overview.failed_7d || 0}</strong><small>需要检查的任务</small></article>
    </div>
    <div class="memory-tabs">${tabs.map(([id, label]) => `<button class="memory-tab ${scheduleView.tab === id ? "active" : ""}" data-schedule-tab="${id}">${label}</button>`).join("")}</div>
    <div class="schedule-body">${body}</div>
  </section>`;
}

function scheduleStats(runs) {
  const stats = {};
  (runs || []).forEach((run) => {
    const key = run.config_key;
    if (!key) return;
    const entry = stats[key] || (stats[key] = { last: null, failed7d: 0, total: 0 });
    entry.total += 1;
    if (!entry.last || (run.started_at || 0) > (entry.last.started_at || 0)) entry.last = run;
    if (run.status === "failed" && (run.started_at || 0) >= Date.now() / 1000 - 604800) entry.failed7d += 1;
  });
  return stats;
}

function renderScheduleJobs(rows, stats = {}) {
  if (!rows.length) return emptyState("还没有定时任务", "点击右上角新建第一个任务。");
  return `<div class="task-table">${rows.map((job) => {
    const stat = stats[job.config_key] || {};
    const last = stat.last;
    return `<article class="task-row">
    <div class="task-main"><div class="record-meta">${scheduleStatus(job)}<span>${escapeHtml(job.source === "config" ? "管理员任务" : "建议模板")}</span><span>${escapeHtml(job.action)}</span>${stat.failed7d ? `<span class="data-badge status-failed">近 7 日失败 ${stat.failed7d}</span>` : ""}</div><h3>${escapeHtml(job.name || job.config_key)}</h3><p>${escapeHtml(scheduleLabel(job))} · 群 ${escapeHtml(job.group_id)}</p><p class="muted">上次执行：${last ? `${formatTime(last.started_at)} · ${last.status === "failed" ? "失败" : last.status === "running" ? "进行中" : "成功"}${last.detail ? ` · ${escapeHtml(String(last.detail).slice(0, 80))}` : ""}` : "还没有运行记录"}</p></div>
    <div class="task-next"><small>下次执行</small><b>${job.enabled ? formatTime(job.next_run) : "—"}</b></div>
    <div class="task-actions">${job.source === "config" ? `<label class="switch task-switch" title="${job.enabled ? "停用" : "启用"}"><input type="checkbox" data-task-toggle="${escapeHtml(job.config_key)}" ${job.enabled ? "checked" : ""}><span></span></label><button data-task-run="${job.id}" class="button subtle">立即执行</button><button data-task-edit="${escapeHtml(job.config_key)}" class="button subtle">编辑</button>` : ""}</div>
  </article>`;
  }).join("")}</div>`;
}

function renderScheduleTimeline(rows) {
  const active = rows.filter((job) => job.enabled).sort((a, b) => a.next_run - b.next_run);
  if (!active.length) return emptyState("没有等待执行的任务", "启用任务后，下次执行时间会显示在这里。");
  return `<div class="timeline-list">${active.map((job) => `<article><time>${formatTime(job.next_run)}</time><i></i><div><b>${escapeHtml(job.name || job.config_key)}</b><p>${escapeHtml(job.action)} · 群 ${escapeHtml(job.group_id)}</p></div></article>`).join("")}</div>`;
}

function renderScheduleRuns(rows) {
  if (!rows.length) return emptyState("还没有运行记录", "任务首次执行后会留下可审计记录。");
  return `<div class="run-list">${rows.map((run) => `<article class="run-card"><div class="run-marker ${escapeHtml(run.status)}"></div><div><div class="record-meta">${statusBadge(run.status)}<span>${escapeHtml(run.trigger === "manual" ? "手动执行" : "定时触发")}</span><span>${escapeHtml(run.action)}</span></div><h3>${escapeHtml(run.config_key)} · 群 ${escapeHtml(run.group_id)}</h3><p>${escapeHtml(run.detail || "执行完成，没有额外信息")}</p><small>${formatTime(run.started_at)}${run.finished_at ? ` · 耗时 ${Math.max(0, run.finished_at - run.started_at)} 秒` : ""}</small></div></article>`).join("")}</div>`;
}

function renderScheduleTemplates(rows) {
  if (!rows.length) return emptyState("没有可用模板", "启用带任务建议的扩展后会在这里出现。");
  return `<div class="card-grid">${rows.map((item) => `<article class="template-card"><div class="record-meta"><span>${escapeHtml(item.action)}</span><span>${escapeHtml(item.kind)}</span></div><h3>${escapeHtml(item.id)}</h3><p>${escapeHtml(item.description || item.prompt)}</p><small>${escapeHtml(item.value)}</small><button data-template-id="${escapeHtml(item.id)}" class="button subtle" ${item.adopted ? "disabled" : ""}>${item.adopted ? "已采纳" : "采纳并配置"}</button></article>`).join("")}</div>`;
}

function jobDraft(job = {}) {
  return {
    id: job.config_key || job.id || "", name: job.name || "", description: job.description || "",
    group_id: job.group_id || scheduleView.overview.groups[0] || "", kind: job.schedule_kind || job.kind || "cron",
    value: job.schedule_value || job.value || "0 12 * * *", action: job.action || "chat", prompt: job.prompt || "",
    payload: job.payload || {}, enabled: job.enabled === undefined ? true : Boolean(job.enabled),
  };
}

async function openScheduleEditor(job = null) {
  // Editing needs the stored definition: the scheduler row omits prompt and
  // payload, and writing it back would silently clear them.
  if (job && job.config_key) {
    try { job = (await api(`/api/schedules/jobs/${encodeURIComponent(job.config_key)}`)).data; }
    catch (error) { toast(error.message || "读取任务失败", true); return; }
  }
  const draft = jobDraft(job || {}), editing = Boolean(job && job.id);
  const groups = scheduleView.overview.groups || [], actions = scheduleView.overview.actions || [];
  showDetail(editing ? "编辑任务" : "新建任务", `<form id="schedule-form" class="task-form">
    <div class="form-grid"><label><span>任务名称</span><input name="name" value="${escapeHtml(draft.name)}" required></label><label><span>任务 ID</span><input name="id" value="${escapeHtml(draft.id)}" ${editing ? "readonly" : ""} required></label>
    <label><span>目标群</span><select name="group_id">${groups.map((value) => `<option ${value === draft.group_id ? "selected" : ""}>${escapeHtml(value)}</option>`).join("")}</select></label><label><span>执行动作</span><select name="action">${actions.map((value) => `<option ${value === draft.action ? "selected" : ""}>${escapeHtml(value)}</option>`).join("")}</select></label>
    <label><span>调度类型</span><select name="kind"><option value="cron" ${draft.kind === "cron" ? "selected" : ""}>Cron</option><option value="every" ${draft.kind === "every" ? "selected" : ""}>固定间隔</option><option value="at" ${draft.kind === "at" ? "selected" : ""}>单次执行</option></select></label><label><span>调度值</span><input name="value" value="${escapeHtml(draft.value)}" required></label></div>
    <label><span>任务说明</span><input name="description" value="${escapeHtml(draft.description)}"></label><label><span>Prompt</span><textarea name="prompt">${escapeHtml(draft.prompt)}</textarea></label><label><span>动作参数（JSON）</span><textarea name="payload">${escapeHtml(JSON.stringify(draft.payload, null, 2))}</textarea></label>
    <label class="form-toggle"><input name="enabled" type="checkbox" ${draft.enabled ? "checked" : ""}><span>启用任务</span></label><div id="schedule-preview" class="schedule-preview"></div>
    <div class="form-actions">${editing ? `<button id="schedule-delete" class="button danger" type="button">删除任务</button>` : ""}<span></span><button class="button subtle" type="button" id="schedule-cancel">取消</button><button class="button primary" type="submit">保存任务</button></div>
  </form>`);
  const form = $("#schedule-form");
  const preview = async () => { try { const result = await api(`/api/schedules/preview?kind=${encodeURIComponent(form.kind.value)}&value=${encodeURIComponent(form.value.value)}`); $("#schedule-preview").textContent = `接下来执行：${result.data.map(formatTime).join(" · ")}`; } catch (error) { $("#schedule-preview").textContent = error.message; } };
  form.kind.onchange = preview; form.value.onchange = preview; preview();
  $("#schedule-cancel").onclick = () => $("#detail-dialog").close();
  if (editing) $("#schedule-delete").onclick = async () => { if (!confirm(`确定删除任务 ${draft.id}？运行历史会保留。`)) return; await api(`/api/schedules/jobs/${encodeURIComponent(draft.id)}`, { method: "DELETE" }); $("#detail-dialog").close(); toast("任务已删除"); renderSchedules(); };
  form.onsubmit = async (event) => { event.preventDefault(); try { const body = Object.fromEntries(new FormData(form)); body.enabled = form.enabled.checked; body.payload = JSON.parse(body.payload || "{}"); const path = editing ? `/api/schedules/jobs/${encodeURIComponent(draft.id)}` : "/api/schedules/jobs"; await api(path, { method: editing ? "PUT" : "POST", body: JSON.stringify(body) }); $("#detail-dialog").close(); toast("任务已保存并立即生效"); renderSchedules(); } catch (error) { toast(error.message || "保存失败", true); } };
}

async function renderSchedules(tab = scheduleView.tab) {
  scheduleView.tab = tab;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "schedules"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden"); history.replaceState(null, "", "#schedules");
  $("#content").innerHTML = `<section class="schedule-workspace loading-state">正在读取任务数据…</section>`;
  try {
    const [overview, jobs, runs] = await Promise.all([api("/api/schedules/overview"), api("/api/schedules/jobs"), api("/api/schedules/runs?limit=200")]);
    scheduleView.overview = overview.data; scheduleView.jobs = jobs.data; scheduleView.runs = runs.data;
    const stats = scheduleStats(runs.data);
    let payload = jobs.data;
    if (tab === "runs") payload = runs.data;
    if (tab === "templates") payload = (await api("/api/schedules/suggestions")).data;
    const body = tab === "jobs" ? renderScheduleJobs(payload, stats) : tab === "timeline" ? renderScheduleTimeline(payload) : tab === "runs" ? renderScheduleRuns(payload) : renderScheduleTemplates(payload);
    $("#content").innerHTML = scheduleChrome(body);
    $("#schedule-create").onclick = () => openScheduleEditor();
    document.querySelectorAll("[data-schedule-tab]").forEach((button) => { button.onclick = () => renderSchedules(button.dataset.scheduleTab); });
    document.querySelectorAll("[data-task-edit]").forEach((button) => { button.onclick = () => openScheduleEditor(scheduleView.jobs.find((item) => item.config_key === button.dataset.taskEdit)); });
    document.querySelectorAll("[data-task-toggle]").forEach((input) => { input.onchange = async () => { const key = input.dataset.taskToggle; try { const full = (await api(`/api/schedules/jobs/${encodeURIComponent(key)}`)).data; await api(`/api/schedules/jobs/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ ...full, enabled: input.checked }) }); toast(input.checked ? "任务已启用" : "任务已停用"); renderSchedules("jobs"); } catch (error) { toast(error.message || "切换失败", true); input.checked = !input.checked; } }; });
    document.querySelectorAll("[data-task-run]").forEach((button) => { button.onclick = async () => { button.disabled = true; try { await api(`/api/schedules/jobs/${button.dataset.taskRun}/run`, { method: "POST", body: "{}" }); toast("任务已提交执行"); setTimeout(() => renderSchedules("runs"), 700); } catch (error) { toast(error.message, true); button.disabled = false; } }; });
    document.querySelectorAll("[data-template-id]").forEach((button) => { button.onclick = async () => { const templates = (await api("/api/schedules/suggestions")).data; const item = templates.find((row) => row.id === button.dataset.templateId); openScheduleEditor(item); }; });
  } catch (error) { $("#content").innerHTML = `<section class="schedule-workspace">${emptyState("任务中心加载失败", error.message || "请稍后重试")}</section>`; }
}

/* Experiment lab --------------------------------------------------------- */
/* Everything here renders a projection the server already computed
   (src/qqbot/interfaces/webui/experiments.py). The browser does no reading of its
   own: the same conclusions drive the standalone HTML page, and deriving them
   in two places is how a dashboard starts lying. */

let experimentView = { tab: "holdout", data: null };

function percent(value, digits = 1) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function signed(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  const text = (value * 100).toFixed(digits);
  return value > 0 ? `+${text}` : text;
}

function experimentBar(value, maximum, level) {
  const width = maximum > 0 ? Math.max(0, Math.min(1, value / maximum)) * 100 : 0;
  return `<div class="exp-bar ${escapeHtml(level)}"><i style="width:${width.toFixed(1)}%"></i></div>`;
}

function renderExperimentHeadline(view) {
  const head = view.headline || {};
  const oracle = head.oracle_value;
  const cards = [
    ["图谱案例", view.graph_cases ?? "—", "编译后的行为图谱规模"],
    ["留出案例", head.held_out_cases ?? "—", `${head.held_out_sessions ?? 0} 个整会话未参与检索`],
    [head.reference_label || "多数类基线", percent(head.reference_value), "任何变体都要先超过它"],
    ["最优变体", percent(head.best_value), head.best_name || ""],
  ];
  const paired = head.best_paired || {};
  const overReference = (head.best_value || 0) - (head.reference_value || 0);
  let verdict;
  if (head.beats_reference && head.best_significant) {
    verdict = `<span class="exp-verdict good">显著超过常数预测器 ${signed(overReference)}（p=${paired.p_value.toFixed(3)}）</span>`;
  } else if (head.beats_reference) {
    verdict = `<span class="exp-verdict warn">高于常数预测器 ${signed(overReference)}，但配对检验 p=${paired.p_value.toFixed(3)}，未达显著</span>`;
  } else {
    verdict = `<span class="exp-verdict bad">仍低于常数预测器，差 ${signed(overReference)}</span>`;
  }
  const tax = head.taxonomy || null;
  const taxonomyChip = tax
    ? `<span class="exp-oracle">标签体系 ${tax.coarse_classes} 类（由 ${tax.fine_classes} 个细类合并，与线上代码一致）</span>`
    : `<span class="exp-oracle">标签体系 原始细类</span>`;
  return `<div class="metric-grid experiment-metrics">
    ${cards.map(([label, value, note]) => `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong><small>${escapeHtml(note)}</small></article>`).join("")}
  </div>
  <div class="experiment-verdict">
    ${verdict}
    ${oracle === null || oracle === undefined ? "" : `<span class="exp-oracle">情境完美时的上限 ${percent(oracle)}（${head.oracle_families ?? 0} 个情境族）</span>`}
    ${taxonomyChip}
  </div>`;
}

function renderExperimentFindings(view) {
  const findings = view.findings || [];
  if (!findings.length) return "";
  return `<section class="exp-findings">
    <div class="panel-title"><h3>结论与下一步</h3><span class="muted">由本次实验数据推出，重新跑实验后自动更新</span></div>
    ${findings.map((item) => `<article class="exp-finding ${escapeHtml(item.level)}">
      <div class="exp-finding-head"><span class="exp-level">${({good: "利好", warn: "注意", bad: "问题", info: "观察"})[item.level] || "观察"}</span><h4>${escapeHtml(item.title)}</h4></div>
      <p>${escapeHtml(item.detail)}</p>
      <div class="exp-action"><b>下一步</b><span>${escapeHtml(item.action)}</span></div>
    </article>`).join("")}
  </section>`;
}

function pairedCell(paired) {
  if (!paired) return "—";
  const color = paired.significant ? "#167057" : (paired.fixed > paired.broke ? "#8A6519" : "#7b8b9c");
  return `<span class="mono" style="color:${color}">${paired.fixed}↑ / ${paired.broke}↓ p=${paired.p_value.toFixed(3)}</span>`;
}

function renderExperimentVariants(protocol) {
  const metric = protocol.metric || {};
  const reference = protocol.reference || {};
  const header = `<tr>
    <th>评分变体</th>
    ${metric.show_coverage ? `<th class="num">覆盖@3</th>` : ""}
    ${metric.secondary_label ? `<th class="num">${escapeHtml(metric.secondary_label)}</th>` : ""}
    <th class="num">${escapeHtml(metric.primary_label)}</th>
    <th class="num">对比现状</th>
    <th class="num">配对检验 vs 纯先验</th>
    <th class="graph">分布</th>
  </tr>`;
  const groups = (protocol.groups || []).map((group) => {
    const rows = group.rows.map((row) => {
      const marker = row.is_baseline ? "基准" : row.level === "better" ? "↑" : row.level === "worse" ? "↓" : "=";
      return `<tr class="${row.is_baseline ? "is-baseline" : ""}">
        <td class="name">${escapeHtml(row.name)}</td>
        ${metric.show_coverage ? `<td class="num">${percent(row.coverage)}</td>` : ""}
        ${metric.secondary_label ? `<td class="num">${percent(row.secondary)}</td>` : ""}
        <td class="num strong">${percent(row.primary)}</td>
        <td class="num delta ${escapeHtml(row.level)}">${marker} <span class="mono">${row.is_baseline ? "—" : signed(row.delta)}</span></td>
        <td class="num">${pairedCell(row.paired)}</td>
        <td class="graph">${experimentBar(row.primary, protocol.maximum, row.level)}</td>
      </tr>`;
    }).join("");
    return `<tr class="exp-group"><th colspan="7">${escapeHtml(group.title)}<span class="blurb">${escapeHtml(group.blurb)}</span></th></tr>${rows}`;
  }).join("");
  const trainNote = reference.train_value === null || reference.train_value === undefined
    ? "" : `（训练侧占 ${percent(reference.train_value)}）`;
  return `<div class="exp-reference">
      <div><span>必须超过的基线</span><b>${percent(reference.value)}</b><small>${escapeHtml(reference.label)}${trainNote}</small></div>
      <p>${escapeHtml(reference.note)}</p>
    </div>
    <table class="exp-table"><thead>${header}</thead><tbody>${groups}</tbody></table>
    <p class="exp-foot">「${escapeHtml(metric.primary_label)}」：${escapeHtml(metric.primary_help)}${metric.secondary_label ? `；「${escapeHtml(metric.secondary_label)}」：${escapeHtml(metric.secondary_help)}` : ""}。「配对检验」只看两个排序在同一批案例上不一致的那些：修好的条数明显多于弄坏的、且 p &lt; 0.05，才算真的赢。</p>`;
}

function renderExperimentSweep(protocol) {
  const sweep = protocol.sweep || [];
  if (sweep.length < 2) return "";
  const peak = sweep.reduce((a, b) => (b.primary > a.primary ? b : a));
  const pure = sweep.find((row) => row.weight === 0);
  const rows = sweep.map((row) => `<tr class="${row === peak ? "is-baseline" : ""}">
      <td class="name mono">λ=${row.weight}</td>
      <td class="num strong">${percent(row.primary)}</td>
      <td class="num">${percent(row.secondary)}</td>
      <td class="num">${pairedCell(row.paired)}</td>
      <td class="graph">${experimentBar(row.primary, protocol.maximum, row === peak ? "better" : "same")}</td>
    </tr>`).join("");
  const gain = peak.primary - (pure ? pure.primary : 0);
  return `<section class="exp-section">
    <div class="panel-title"><h3>先验校准曲线</h3><span class="muted">λ=0 是构造出来的常数预测器，是这条曲线的起点</span></div>
    <table class="exp-table">
      <thead><tr><th>权重</th><th class="num">Top-1 命中</th><th class="num">任意@3</th><th class="num">配对检验 vs λ=0</th><th class="graph">分布</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="exp-foot">峰值在 λ=${peak.weight}，${percent(peak.primary)}，比纯先验高 ${percent(gain)}。λ 越大越像纯检索；曲线两端都低、中间有峰，说明检索和先验各自都缺一半信息。</p>
  </section>`;
}

function renderExperimentPerClass(protocol) {
  const table = protocol.per_class || { rows: [] };
  const best = protocol.per_class_best || { rows: [] };
  if (!table.rows.length) return "";
  const bestByStrategy = new Map(best.rows.map((row) => [row.strategy, row]));
  const rows = table.rows.map((row) => {
    const other = bestByStrategy.get(row.strategy) || {};
    const lift = row.lift || 0;
    return `<tr>
      <td class="name">${escapeHtml(row.strategy)}</td>
      <td class="num">${percent(row.share)}</td>
      <td class="num strong">${percent(row.recall_top1)}</td>
      <td class="num">${percent(row.precision_top1)}</td>
      <td class="num" style="color:${lift > 0.02 ? "#167057" : lift < 0 ? "#9B3750" : "#7b8b9c"}">${signed(lift)}</td>
      <td class="num">${percent(row.recall_any3)}</td>
      <td class="num">${percent(other.recall_top1)}</td>
      <td class="graph">${experimentBar(row.precision_top1, 1, lift > 0 ? "better" : "worse")}</td>
    </tr>`;
  }).join("");
  return `<section class="exp-section">
    <div class="panel-title"><h3>分类召回与精确率</h3><span class="muted">聚合准确率会掩盖失败模式，逐类看才看得出检索到底认出了什么</span></div>
    <table class="exp-table">
      <thead><tr>
        <th>策略</th><th class="num">占比</th><th class="num">Top-1 召回</th>
        <th class="num">精确率</th><th class="num">提升</th>
        <th class="num">任意@3 召回</th><th class="num">最优变体召回</th><th class="graph">精确率 vs 占比</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="exp-foot">基准变体：${escapeHtml(table.variant || "")}；最优变体：${escapeHtml(best.variant || "")}。<b>召回</b>高于占比说明这一类被认出来了；<b>精确率</b>高于占比（即「提升」为正）说明「预测成这一类」本身带信息，而不是靠先验凑数。提升为负的类等于从未被正确预测过。</p>
  </section>`;
}

function renderExperimentShift(protocol) {
  const shift = protocol.prediction_shift || { rows: [] };
  if (!shift.rows.length) return "";
  const rows = shift.rows.map((row) => `<tr>
      <td class="name">${escapeHtml(row.strategy)}</td>
      <td class="num">${percent(row.prior)}</td>
      <td class="num">${percent(row.baseline)}</td>
      <td class="num">${percent(row.best)}</td>
      <td class="graph">${experimentBar(row.best, 1, row.best > row.prior + 0.05 ? "worse" : "same")}</td>
    </tr>`).join("");
  return `<section class="exp-section">
    <div class="panel-title"><h3>预测分布 vs 真实占比</h3><span class="muted">用来判断准确率的上涨是不是只靠多猜最大类</span></div>
    <table class="exp-table">
      <thead><tr><th>策略</th><th class="num">训练侧占比</th><th class="num">基准预测</th><th class="num">最优预测</th><th class="graph">偏移</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="exp-foot">「训练侧占比」是这一类真实出现的比例。预测比例明显高于它，说明系统在往先验上塌。</p>
  </section>`;
}

function renderExperimentFolds(protocol) {
  const folds = protocol.per_fold || [];
  if (folds.length < 2) return "";
  const agree = folds.filter((fold) => fold.beats_constant).length;
  const rows = folds.map((fold) => `<tr>
      <td class="name">折 ${fold.fold}</td>
      <td class="num">${fold.held_out_cases}</td>
      <td class="num">${fold.train_cases}</td>
      <td class="num">${percent(fold.constant)}</td>
      <td class="num strong">${percent(fold.best)}</td>
      <td class="num" style="color:${fold.beats_constant ? "#167057" : "#9B3750"}">${signed(fold.edge)}</td>
      <td class="name mono">${escapeHtml(fold.best_name)}</td>
    </tr>`).join("");
  return `<section class="exp-section">
    <div class="panel-title"><h3>每折结果</h3><span class="muted">${folds.length} 折里 ${agree} 折的最优变体高于该折的常数预测器</span></div>
    <table class="exp-table">
      <thead><tr>
        <th>折</th><th class="num">评测条数</th><th class="num">训练条数</th>
        <th class="num">该折常数预测器</th><th class="num">该折最优</th><th class="num">差距</th><th>最优变体</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="exp-foot">折之间训练集互不相同，但评测覆盖全部案例且互不重叠。方向一致才说明结论不是划分方式带来的偶然。</p>
  </section>`;
}

function renderExperimentProtocol(protocol) {
  const head = protocol.counts || {};
  const split = protocol.id === "holdout_strategy" && head.folds
    ? `<div class="exp-split">
        <span>${head.folds} 折交叉验证</span>
        <span>每折训练约 ${head.train_cases} 条</span>
        <span>共评测 ${head.held_out_cases} 条 / ${head.held_out_sessions} 个会话</span>
      </div>`
    : "";
  return `<section class="exp-protocol">
    <div class="exp-protocol-head">
      <div><h3>${escapeHtml(protocol.title)}</h3><p class="muted">${escapeHtml(protocol.description)}</p></div>
      <span class="section-count">${escapeHtml(protocol.headline)}</span>
    </div>
    ${split}
    ${renderExperimentVariants(protocol)}
    ${protocol.id === "holdout_strategy" ? renderExperimentSweep(protocol) + renderExperimentFolds(protocol) + renderExperimentPerClass(protocol) + renderExperimentShift(protocol) : ""}
  </section>`;
}

function renderExperimentMethod(view) {
  const oracle = view.headline?.oracle_value;
  return `<section class="exp-protocol">
    <div class="exp-protocol-head"><div><h3>指标说明</h3><p class="muted">这张看板为什么这样设计</p></div></div>
    <ol class="exp-method">
      <li><b>只看第一个协议。</b>第二个协议复现的是旧指标：它的期望标签来自关键词表，兜底桶占了 ${percent(view.protocols[1]?.reference?.value ?? 0)} 以上，本身不是正确性标准。</li>
      <li><b>先看常数预测器。</b>${escapeHtml(view.headline?.reference_label || "")}在留出集上是 ${percent(view.headline?.reference_value)}。这是「不看问题、永远回答同一个策略」的成绩，也是检索必须先超过的线。</li>
      <li><b>再看先验校准曲线。</b>λ=0 是构造出来的常数预测器，所以这条曲线从基线出发。两端都低、中间有峰，说明检索和先验各自都缺一半信息；整条线都不超过 λ=0 就说明检索没有价值。</li>
      <li><b>看配对检验，不要只看准确率。</b>两个排序跑在同一批案例上，只有它们<b>不一致</b>的那些才提供证据：修好的条数多于弄坏的、且 p &lt; 0.05，才算真的赢。</li>
      <li><b>再看分类召回与精确率。</b>聚合准确率会掩盖失败模式：一个只会预测最大类的系统能拿到与先验持平的分数，却在其它类上完全无用。精确率高于占比，才说明「预测成这一类」本身带信息。</li>
      <li><b>最后看字段拆分。</b>「只看话题/情境文本」和「只看互动线索文本」这两行直接回答「检索靠哪半边在匹配」。</li>
      ${oracle === null || oracle === undefined ? "" : `<li><b>情境上限。</b>「若情境分类完全正确」是图谱自身情境→策略统计能达到的天花板 ${percent(oracle)}。它衡量的是分类体系给得起多少空间，不是当前能拿到多少。</li>`}
      <li><b>数据来自文件。</b>看板读的是 <span class="mono">${escapeHtml(view.path || "")}</span>，由 <span class="mono">${escapeHtml(view.command || "")}</span> 生成。重跑实验再刷新即可更新。</li>
    </ol>
  </section>`;
}

function experimentChrome(view, body) {
  const tabs = [["holdout", "真实留出评估"], ["legacy", "旧指标复现"], ["method", "指标说明"]];
  return `<section class="experiment-workspace">
    <div class="experiment-toolbar">
      <div><h2>实验看板</h2></div>
      <span class="exp-source mono">${escapeHtml(view.path || "")}</span>
    </div>
    ${view.headline && Object.keys(view.headline).length ? renderExperimentHeadline(view) : ""}
    ${renderExperimentFindings(view)}
    <div class="memory-tabs">${tabs.map(([id, label]) => `<button class="memory-tab ${experimentView.tab === id ? "active" : ""}" data-experiment-tab="${id}">${label}</button>`).join("")}</div>
    <div class="experiment-body">${body}</div>
  </section>`;
}

async function renderExperiments(tab = experimentView.tab) {
  experimentView.tab = tab;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "experiments"));
  $(".search").classList.add("hidden");
  $("#save").classList.add("hidden");
  history.replaceState(null, "", "#experiments");
  $("#content").innerHTML = `<section class="experiment-workspace loading-state">正在读取实验数据…</section>`;
  try {
    const view = (await api("/api/experiments/behavior-retrieval")).data;
    if (!view.available) {
      $("#content").innerHTML = `<section class="experiment-workspace">${emptyState(view.reason || "没有实验数据", `${view.detail || ""} 生成命令：${view.command || ""}`)}</section>`;
      return;
    }
    experimentView.data = view;
    const protocol = view.protocols.find((item) => item.id === "holdout_strategy") || view.protocols[0];
    const legacy = view.protocols.find((item) => item.id === "legacy_family");
    const body = tab === "legacy" ? (legacy ? renderExperimentProtocol(legacy) : emptyState("没有旧指标数据", "本次报告里没有 legacy_family 协议。"))
      : tab === "method" ? renderExperimentMethod(view)
      : renderExperimentProtocol(protocol);
    $("#content").innerHTML = experimentChrome(view, body);
    document.querySelectorAll("[data-experiment-tab]").forEach((button) => {
      button.onclick = () => renderExperiments(button.dataset.experimentTab);
    });
  } catch (error) {
    $("#content").innerHTML = `<section class="experiment-workspace">${emptyState("实验看板加载失败", error.message || "请稍后重试")}</section>`;
  }
}



let skillView = { q: "", state: "", selected: "", mode: "edit", rows: [], meta: null, dirty: false };

const SKILL_STATES = {
  enabled: { label: "启用中", tone: "ok" },
  disabled: { label: "已停用", tone: "muted" },
  unavailable: { label: "扩展未启用", tone: "warn" },
  broken: { label: "有问题", tone: "bad" },
};

function skillStatePill(row) {
  const meta = SKILL_STATES[row.state] || SKILL_STATES.broken;
  const hint = row.reason ? ` title="${escapeHtml(row.reason)}"` : "";
  return `<span class="state-pill ${meta.tone}"${hint}>${meta.label}</span>`;
}

function skillTriggerCell(row) {
  if (!row.triggers.length) return `<span class="muted">${row.always_on ? "常驻，无需触发词" : "无触发词"}</span>`;
  const shown = row.triggers.slice(0, 2).map((item) => `<span class="trig">${escapeHtml(item)}</span>`).join("");
  const rest = row.triggers.length > 2 ? `<span class="trig more">+${row.triggers.length - 2}</span>` : "";
  return `<span class="trig-row">${shown}${rest}</span>`;
}

function skillRow(row) {
  const active = row.name === skillView.selected ? " active" : "";
  const tags = [
    row.always_on ? `<span class="mini-tag">常驻</span>` : "",
    row.priority ? `<span class="mini-tag">优先级 ${row.priority}</span>` : "",
    row.adapter ? `<span class="mini-tag">adapter</span>` : "",
    row.audiences.length && !(row.audiences.length === 1 && row.audiences[0] === "group")
      ? `<span class="mini-tag">${row.audiences.map(escapeHtml).join("/")}</span>` : "",
  ].join("");
  const search = `${row.name} ${row.description} ${row.triggers.join(" ")}`.toLowerCase();
  const toggle = row.state === "unavailable" || row.state === "broken"
    ? `<button class="button subtle mini" type="button" disabled title="${escapeHtml(row.reason)}">—</button>`
    : `<button class="button subtle mini" type="button" data-skill-toggle="${escapeHtml(row.name)}" data-next="${row.state === "enabled" ? "false" : "true"}">${row.state === "enabled" ? "停用" : "启用"}</button>`;
  return `<tr class="skill-row${active}" data-skill-row="${escapeHtml(row.name)}" data-skill-search="${escapeHtml(search)}" data-skill-state="${row.state}">
    <td><div class="skill-name"><b>${escapeHtml(row.name)}</b><small>${escapeHtml(row.description || "（没有描述）")}</small><div class="mini-tags">${tags}</div></div></td>
    <td>${skillStatePill(row)}</td>
    <td>${skillTriggerCell(row)}</td>
    <td class="num">${row.calls}</td>
    <td>${toggle}</td>
  </tr>`;
}

function skillEditor(detail) {
  const creating = skillView.mode === "create";
  const row = detail || { name: "", description: "", triggers: [], priority: 0, audiences: ["group"], always_on: false, body: "", state: "enabled", reason: "", issues: [], adapter_config: {}, path: "" };
  const disabled = !creating && (row.state === "unavailable" || row.state === "broken");
  const reason = !creating && row.reason
    ? `<div class="notice-block"><b>为什么现在不生效</b><p>${escapeHtml(row.reason)}</p>${row.issues.length ? `<ul>${row.issues.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}</div>`
    : "";
  const adapter = row.adapter_config && Object.keys(row.adapter_config).length
    ? `<div class="notice-block subtle"><b>这个 Skill 由 bot-adapter.json 接管</b><p>运行时只取 ${(row.adapter_config.sections || []).map((name) => `「${escapeHtml(name)}」`).join("、") || "整个文件"}${row.adapter_config.body_file ? `，外加 ${escapeHtml(row.adapter_config.body_file)}` : ""}${row.adapter_config.max_chars ? `，最多 ${row.adapter_config.max_chars} 字` : ""}。改正文时这些小节标题必须保留。</p></div>`
    : "";
  const audienceBox = (id, label) => `<label class="check"><input type="checkbox" data-field="audiences" value="${id}" ${row.audiences.includes(id) ? "checked" : ""} ${disabled ? "disabled" : ""}><span>${label}</span></label>`;
  return `<div class="memory-toolbar"><div><h2>${escapeHtml(row.name || "新建 Skill")}${creating ? "" : ` <span class="state-pill ${(SKILL_STATES[row.state] || SKILL_STATES.broken).tone}">${(SKILL_STATES[row.state] || SKILL_STATES.broken).label}</span>`}</h2><p class="muted">${creating ? "保存后会写入 skills/<名称>/SKILL.md。" : escapeHtml(row.path || "")}</p></div></div>
    ${reason}${adapter}
    <div class="editor-grid">
      <label class="editor-field"><span>名称</span><input data-field="name" value="${escapeHtml(row.name)}" ${creating ? "" : "readonly"} placeholder="小写字母、数字、-"></label>
      <label class="editor-field wide"><span>描述（模型只看到这一句）</span><input data-field="description" value="${escapeHtml(row.description)}" ${disabled ? "disabled" : ""} placeholder="一句话说明什么时候该用它"></label>
      <label class="editor-field wide"><span>触发词（逗号分隔）</span><input data-field="triggers" value="${escapeHtml(row.triggers.join(", "))}" ${disabled ? "disabled" : ""} placeholder="群聊, 聊天, @"></label>
      <label class="editor-field"><span>优先级</span><input data-field="priority" type="number" min="-100" max="100" value="${row.priority}" ${disabled ? "disabled" : ""}></label>
      <div class="editor-field"><span>作用域</span><div class="check-row">${audienceBox("group", "群聊")}${audienceBox("private", "私聊")}</div></div>
      <label class="check wide"><input type="checkbox" data-field="always_on" ${row.always_on ? "checked" : ""} ${disabled ? "disabled" : ""}><span>常驻：每轮都注入，不靠触发词</span></label>
      <label class="editor-field wide"><span>正文（给模型的指令）</span><textarea data-field="body" rows="12" ${disabled ? "disabled" : ""}>${escapeHtml(row.body)}</textarea></label>
    </div>
    <div class="editor-actions">
      <button class="button primary" type="button" id="skill-save" ${disabled ? "disabled" : ""}>${creating ? "创建" : "保存"}</button>
      <button class="button subtle" type="button" id="skill-cancel">取消</button>
      ${creating ? "" : `<button class="button subtle" type="button" id="skill-archive">归档（移到 .archive）</button>`}
      <span class="editor-dirty hidden" id="skill-dirty">未保存</span><span class="editor-error" id="skill-error"></span>
    </div>
    ${creating ? "" : `<details class="raw-box"><summary>查看磁盘上的 SKILL.md 原文</summary><pre>${escapeHtml(detail.raw || "")}</pre></details>`}`;
}

function confirmDiscard() {
  if (!skillView.dirty) return true;
  return window.confirm("这个 Skill 还有没保存的修改，确定放弃？");
}

async function openSkill(name, mode = "edit") {
  const target = mode === "create" ? "" : name;
  if (skillView.selected !== target && !confirmDiscard()) return;
  skillView.dirty = false;
  skillView.selected = target;
  skillView.mode = mode;
  const panel = $("#skill-detail");
  if (!panel) return;
  panel.classList.remove("hidden");
  const layout = panel.closest(".skill-layout");
  if (layout) layout.classList.add("has-selection");
  if (mode === "create") {
    panel.innerHTML = skillEditor(null);
    bindSkillEditor();
    return;
  }
  panel.innerHTML = `<section class="loading-state">正在读取 ${escapeHtml(name)}…</section>`;
  try {
    const { data } = await api(`/api/skills/admin/${encodeURIComponent(name)}`);
    panel.innerHTML = skillEditor(data);
    bindSkillEditor();
    document.querySelectorAll("[data-skill-row]").forEach((row) => row.classList.toggle("active", row.dataset.skillRow === name));
  } catch (error) {
    panel.innerHTML = emptyState("读不到这个 Skill", error.message);
  }
}

function skillPayload() {
  const field = (name) => document.querySelector(`[data-field="${name}"]`);
  const audiences = [...document.querySelectorAll('[data-field="audiences"]:checked')].map((item) => item.value);
  return {
    name: (field("name")?.value || "").trim(),
    description: (field("description")?.value || "").trim(),
    triggers: (field("triggers")?.value || "").split(/[,，]/).map((item) => item.trim()).filter(Boolean),
    priority: Number(field("priority")?.value || 0),
    audiences,
    always_on: Boolean(field("always_on")?.checked),
    body: (field("body")?.value || "").trim(),
  };
}

function bindSkillEditor() {
  const panel = $("#skill-detail");
  const markDirty = () => {
    skillView.dirty = true;
    $("#skill-dirty")?.classList.remove("hidden");
  };
  panel.querySelectorAll("[data-field]").forEach((field) => {
    field.addEventListener("input", markDirty);
    field.addEventListener("change", markDirty);
  });
  panel.onkeydown = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      $("#skill-save")?.click();
    } else if (event.key === "Escape") {
      event.preventDefault();
      $("#skill-cancel")?.click();
    }
  };
  const fail = (message) => {
    const box = $("#skill-error");
    if (box) box.textContent = message;
    toast(message, true);
  };
  const save = $("#skill-save");
  if (save) save.onclick = async () => {
    const payload = skillPayload();
    const creating = skillView.mode === "create";
    save.disabled = true;
    try {
      const { data } = creating
        ? await api("/api/skills/admin", { method: "POST", body: JSON.stringify(payload) })
        : await api(`/api/skills/admin/${encodeURIComponent(skillView.selected)}`, { method: "PUT", body: JSON.stringify(payload) });
      skillView.dirty = false;
      toast(creating ? `已创建 ${data.created}` : `已保存 ${data.saved}`);
      await renderSkills();
      await openSkill(creating ? data.created : skillView.selected);
    } catch (error) {
      fail(error.message);
    } finally {
      save.disabled = false;
    }
  };
  const cancel = $("#skill-cancel");
  if (cancel) cancel.onclick = () => {
    skillView.mode = "edit";
    if (skillView.selected) {
      openSkill(skillView.selected);
      return;
    }
    const panel = $("#skill-detail");
    panel.classList.add("hidden");
    panel.closest(".skill-layout")?.classList.remove("has-selection");
  };
  const archive = $("#skill-archive");
  if (archive) archive.onclick = async () => {
    if (!window.confirm(`归档 ${skillView.selected}？目录会移到 skills/.archive，不会删除。`)) return;
    try {
      const { data } = await api(`/api/skills/admin/${encodeURIComponent(skillView.selected)}/archive`, { method: "POST", body: "{}" });
      toast(`已归档到 ${data.path}`);
      skillView.selected = "";
      await renderSkills();
    } catch (error) {
      fail(error.message);
    }
  };
}

async function renderSkills() {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "skills"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden"); history.replaceState(null, "", "#skills");
  const content = $("#content");
  if (!skillView.rows.length) content.innerHTML = `<section class="memory-workspace loading-state">正在读取 skills/ 目录…</section>`;
  try {
    const { data } = await api("/api/skills/admin");
    skillView.rows = data.skills || [];
    skillView.meta = data;
    const chip = (id, label) => `<button class="chip ${skillView.state === id ? "active" : ""}" data-skill-filter="${id}">${label}${id ? ` ${data.counts[id] ?? 0}` : ` ${data.total}`}</button>`;
    content.innerHTML = `<section class="memory-workspace">
      <div class="memory-toolbar"><div><h2>Skill 管理</h2><p class="muted">读的是 ${escapeHtml(data.root)}/ 目录本身：装了什么、为什么没生效、谁在常驻。保存后立即重载目录，不用重启。</p></div>
        <div class="monitor-actions"><span class="data-badge" id="skill-count">${data.total} 个 Skill</span><button class="button subtle" type="button" id="skill-new">新建 Skill</button><button class="button subtle" type="button" id="skill-import">导入 SKILL.md</button><input id="skill-file" type="file" accept=".md,text/markdown,text/plain" hidden></div></div>
      <div class="filter-row"><input id="skill-search" type="search" placeholder="搜索名称、描述或触发词…" value="${escapeHtml(skillView.q)}">${chip("", "全部")}${chip("enabled", "启用中")}${chip("disabled", "已停用")}${chip("unavailable", "扩展未启用")}${chip("broken", "有问题")}</div>
      ${data.allow_list_known ? "" : `<p class="inline-error">当前没有绑定运行中的 Bot，无法判断哪些 Skill 被扩展白名单挡住。</p>`}
      <div class="overview-grid skill-layout${skillView.selected || skillView.mode === "create" ? " has-selection" : ""}">
        <article class="data-panel"><div class="panel-title"><h3>目录</h3><p>近 7 天调用共 ${data.calls_7d} 次。状态取自磁盘和扩展白名单，不从聊天内容推断。</p></div>
          <div class="table-scroll"><table class="skill-table"><thead><tr><th>Skill</th><th>状态</th><th>触发</th><th class="num">调用</th><th>操作</th></tr></thead><tbody id="skill-rows">${skillView.rows.map(skillRow).join("") || `<tr><td colspan="5">${emptyState("skills/ 目录是空的", "用「新建 Skill」或「导入 SKILL.md」放进去一个。")}</td></tr>`}</tbody></table></div>
          ${data.archived.length ? `<p class="muted small">已归档 ${data.archived.length} 个：${data.archived.slice(0, 4).map((item) => escapeHtml(item.name)).join("、")}${data.archived.length > 4 ? "…" : ""}</p>` : ""}
        </article>
        <article class="data-panel skill-detail-panel${skillView.selected || skillView.mode === "create" ? "" : " hidden"}" id="skill-detail">${emptyState("选择一个 Skill", "点左边任意一行查看正文、触发词和启停状态。")}</article>
      </div></section>`;
    const apply = () => {
      let visible = 0;
      document.querySelectorAll("[data-skill-row]").forEach((row) => {
        const matchesText = !skillView.q || row.dataset.skillSearch.includes(skillView.q.toLowerCase());
        const matchesState = !skillView.state || row.dataset.skillState === skillView.state;
        const show = matchesText && matchesState;
        row.classList.toggle("hidden", !show);
        if (show) visible += 1;
      });
      $("#skill-count").textContent = `${visible} / ${skillView.rows.length} 个 Skill`;
    };
    $("#skill-search").oninput = (event) => { skillView.q = event.currentTarget.value.trim(); apply(); };
    document.querySelectorAll("[data-skill-filter]").forEach((button) => {
      button.onclick = () => {
        skillView.state = button.dataset.skillFilter;
        document.querySelectorAll("[data-skill-filter]").forEach((chip) => chip.classList.toggle("active", chip === button));
        apply();
      };
    });
    document.querySelectorAll("[data-skill-row]").forEach((row) => {
      row.onclick = (event) => {
        if (event.target.closest("[data-skill-toggle]")) return;
        openSkill(row.dataset.skillRow);
      };
    });
    document.querySelectorAll("[data-skill-toggle]").forEach((button) => {
      button.onclick = async (event) => {
        event.stopPropagation();
        button.disabled = true;
        try {
          const { data } = await api(`/api/skills/admin/${encodeURIComponent(button.dataset.skillToggle)}/enabled`, { method: "POST", body: JSON.stringify({ enabled: button.dataset.next === "true" }) });
          toast(data.runtime_effect);
          await renderSkills();
        } catch (error) {
          toast(error.message, true);
          button.disabled = false;
        }
      };
    });
    $("#skill-new").onclick = () => openSkill("", "create");
    $("#skill-import").onclick = () => $("#skill-file").click();
    $("#skill-file").onchange = async (event) => {
      const file = event.currentTarget.files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        const { data } = await api("/api/skills/admin/import", { method: "POST", body: JSON.stringify({ filename: file.name, content: text }) });
        toast(`已导入 ${data.created}`);
        await renderSkills();
        await openSkill(data.created);
      } catch (error) {
        toast(error.message, true);
      } finally {
        event.currentTarget.value = "";
      }
    };
    apply();
    mountView(content, "skills");
    if (skillView.selected) openSkill(skillView.selected);
  } catch (error) {
    content.innerHTML = `<section class="memory-workspace">${emptyState("Skill 管理不可用", error.message)}</section>`;
  }
}

let wikiView = { scope: "", q: "" };

function wikiFreshness(seconds) {
  if (!seconds) return "无时间记录";
  const age = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (age < 3600) return `${Math.round(age / 60)} 分钟前更新`;
  if (age < 86400) return `${Math.round(age / 3600)} 小时前更新`;
  return `${Math.round(age / 86400)} 天前更新`;
}

async function renderWikiAdmin(scope = wikiView.scope) {
  wikiView.scope = scope;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "wiki-admin"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden"); history.replaceState(null, "", "#wiki-admin");
  const content = $("#content"); content.innerHTML = `<section class="memory-workspace loading-state">正在读取 LM Wiki Markdown…</section>`;
  try {
    const {data} = await api(`/api/wiki/overview?limit=200${scope ? `&scope=${encodeURIComponent(scope)}` : ""}`);
    let reason = "";
    if (!data.available || !data.pages.length) {
      try {
        const routes = (await api("/api/routes/overview")).data;
        reason = data.available
          ? `Wiki 已启用，但目录 ${routes.wiki.root || "未配置"} ${routes.wiki.root_exists ? "里还没有提炼发布的页面" : "不存在"}。`
          : `Wiki 当前未启用（BOT_WIKI_ENABLED=${routes.wiki.enabled}）。开启后需要重启 Bot，并完成一次提炼发布。`;
      } catch (error) { reason = "无法读取 Wiki 开关状态。"; }
    }
    const scopes = (data.scopes || []).map((item) => `<button class="chip ${scope === item ? "active" : ""}" data-wiki-scope="${escapeHtml(item)}">${escapeHtml(item)}</button>`).join("");
    const pages = data.pages.map((page) => `<button data-wiki-page="${escapeHtml(page.page_id)}" data-wiki-search="${escapeHtml(`${page.title} ${page.page_id}`.toLowerCase())}"><span><b>${escapeHtml(page.title)}</b><small>${escapeHtml(page.scope)} · ${page.facts} 事实 · ${page.topics} 主题 · ${page.slang} 黑话 · ${wikiFreshness(page.latest_observed_at)}</small></span><strong>${escapeHtml(page.version || "—")}</strong></button>`).join("");
    const renderPage = (page) => {
      const detail = $("#wiki-admin-detail");
      if (!detail) return;
      detail.innerHTML = page ? `<h3>${escapeHtml(page.title)}</h3><p class="muted">${escapeHtml(page.page_id)} · ${escapeHtml(page.version || "无版本")} · ${wikiFreshness((page.entries || []).reduce((latest, entry) => Math.max(latest, entry.observed_at || 0), 0))}</p><p>${escapeHtml(page.summary || "暂无摘要")}</p><div class="wiki-facts">${(page.entries || []).slice(0, 30).map((entry) => `<article><b>${escapeHtml(entry.claim)}</b><small>${entry.observed_at ? formatTime(entry.observed_at) : "时间未记录"} · 置信度 ${entry.confidence ?? "未知"}</small></article>`).join("") || `<p class="muted">暂无已发布事实。</p>`}</div><h4>主题 / 黑话 / 链接</h4><p class="muted">${(page.topics || []).length} 个主题 · ${(page.slang || []).length} 条黑话 · ${(page.links || []).length} 条出链 · ${(page.backlinks || []).length} 条反向链接</p>${(page.backlinks || []).length ? `<div class="chip-row">${page.backlinks.map((link) => `<button class="chip" data-wiki-page="${escapeHtml(link.page_id)}">↩ ${escapeHtml(link.title)}</button>`).join("")}</div>` : ""}` : emptyState("选择一个 Wiki 页面", "右侧会显示实际 Markdown 提炼出的实体内容。");
      bindPageButtons();
    };
    const bindPageButtons = () => {
      document.querySelectorAll("[data-wiki-page]").forEach((button) => {
        button.onclick = async () => {
          document.querySelectorAll("[data-wiki-page]").forEach((item) => item.classList.remove("active"));
          button.classList.add("active");
          try { renderPage((await api(`/api/wiki/pages/${encodeURIComponent(button.dataset.wikiPage)}`)).data); }
          catch (error) { $("#wiki-admin-detail").innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`; }
        };
      });
    };
    content.innerHTML = `<section class="memory-workspace"><div class="memory-toolbar"><div><h2>Wiki 管理</h2><p class="muted">管理已提炼发布的群、人物和私聊实体，不重新构造图谱。</p></div><span class="data-badge">${data.available ? `${data.total} 个页面` : "未启用"}</span></div>
      <div class="filter-row"><input id="wiki-search" type="search" placeholder="搜索页面标题或实体 ID…" value="${escapeHtml(wikiView.q)}"><button class="chip ${scope ? "" : "active"}" data-wiki-scope="">全部</button>${scopes}</div>
      ${reason ? `<div class="notice-block"><b>为什么是空的</b><p>${escapeHtml(reason)}</p><button class="button subtle" type="button" id="wiki-open-routing">去链路与图谱开启</button></div>` : ""}
      <div class="memory-body"><div class="wiki-admin-layout"><article class="data-panel"><div class="panel-title"><h3>已发布页面</h3><p>按实体页面倒序浏览，页面内容来自 Markdown 发布目录。</p></div><div class="wiki-page-list">${pages || emptyState(data.available ? "暂无 Wiki 页面" : "Wiki 未启用", data.available ? "启用 Wiki 并完成一次提炼发布后，页面会出现在这里。" : "先在链路与图谱里开启 Wiki。")}</div></article><article class="data-panel" id="wiki-admin-detail">${data.pages.length ? emptyState("选择一个 Wiki 页面", "右侧会显示实际 Markdown 提炼出的实体内容。") : ""}</article></div></div></section>`;
    bindPageButtons();
    document.querySelectorAll("[data-wiki-scope]").forEach((button) => { button.onclick = () => renderWikiAdmin(button.dataset.wikiScope); });
    const routingLink = $("#wiki-open-routing");
    if (routingLink) routingLink.onclick = () => activateSection("routing");
    $("#wiki-search").oninput = (event) => {
      wikiView.q = event.currentTarget.value.trim().toLowerCase();
      document.querySelectorAll("[data-wiki-search]").forEach((button) => {
        button.classList.toggle("hidden", Boolean(wikiView.q) && !button.dataset.wikiSearch.includes(wikiView.q));
      });
    };
  } catch (error) { content.innerHTML = `<section class="memory-workspace">${emptyState("Wiki 管理不可用", error.message)}</section>`; }
}

async function renderDashboard(tab = "overview") {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "dashboard"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden");
  history.replaceState(null, "", `#dashboard${tab === "overview" ? "" : `/${tab}`}`);
  const content = $("#content"); content.innerHTML = `<section class="memory-workspace loading-state">正在读取运行看板…</section>`;
  try {
    const overview = (await api("/api/dashboard/overview?hours=24")).data;
    const tabs = [["overview", "运行总览"], ["traces", "消息 Trace"], ["wiki", "Wiki 图谱"]];
    const tabsHtml = tabs.map(([id, label]) => `<button class="memory-tab ${tab === id ? "active" : ""}" data-dashboard-tab="${id}">${label}</button>`).join("");
    let body = `<div class="metric-grid"><article class="metric-card"><span>接收消息</span><strong>${overview.turns ?? 0}</strong><small>已回复 ${overview.replies ?? 0} · 异常 ${overview.errors ?? 0}</small></article><article class="metric-card"><span>模型 Token</span><strong>${(overview.prompt_tokens ?? 0) + (overview.completion_tokens ?? 0)}</strong><small>输入 ${overview.prompt_tokens ?? 0} / 输出 ${overview.completion_tokens ?? 0}</small></article><article class="metric-card"><span>前缀缓存命中率</span><strong>${typeof overview.cache_hit_ratio === "number" ? `${(overview.cache_hit_ratio * 100).toFixed(1)}%` : "未知"}</strong><small>已报告 ${overview.cache_known_calls ?? 0} 次 · 未知 ${overview.cache_unknown_calls ?? 0} 次</small></article><article class="metric-card"><span>端到端耗时 P95</span><strong>${overview.latency_p95_ms == null ? "未知" : `${overview.latency_p95_ms} ms`}</strong><small>包含不回复消息</small></article></div>`;
    if (tab === "overview") {
      const skillRows = Object.entries(overview.skill_counts || {}).sort((a, b) => b[1] - a[1]).slice(0, 10);
      const toolRows = Object.entries(overview.tool_counts || {}).sort((a, b) => b[1] - a[1]).slice(0, 10);
      const toolOutcomes = Object.entries(overview.tool_outcomes || {}).sort((a, b) => b[1] - a[1]);
      const frequencyList = (rows, empty) => rows.length ? rows.map(([name, count]) => `<li><span>${escapeHtml(outcomeLabel(name))}</span><strong>${count} 次</strong></li>`).join("") : `<li class="muted">${escapeHtml(empty)}</li>`;
      body += `<div class="overview-grid"><article class="data-panel"><div class="panel-title"><h3>Skill 触发频率</h3><p>${overview.skill_turns ?? 0} 个消息触发了 Skill；重复选中按实际次数统计。</p></div><ul class="frequency-list">${frequencyList(skillRows, "当前时间范围没有 Skill 记录")}</ul></article><article class="data-panel"><div class="panel-title"><h3>工具调用频率</h3><p>${overview.tool_calls ?? 0} 次调用 · ${toolOutcomes.map(([name, count]) => `${escapeHtml(name)} ${count}`).join(" · ") || "暂无结果"}</p></div><ul class="frequency-list">${frequencyList(toolRows, "当前时间范围没有工具调用")}</ul></article></div>`;
      const rows = (await api("/api/dashboard/traces?hours=24&limit=8")).data.items || [];
      body += `<article class="data-panel"><div class="panel-title"><h3>最近消息</h3><p>每条记录包含准入、技能、模型调用和发送结果</p></div><div class="table-scroll"><table><thead><tr><th>时间</th><th>会话</th><th>结果</th><th>模型调用</th><th>耗时</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${escapeHtml(formatTime(row.created_at))}</td><td>${escapeHtml(row.scope)}</td><td>${statusBadge(row.outcome)}</td><td>${row.steps.filter((step) => step.stage === "model").length}</td><td>${row.duration_ms ?? "—"} ms</td></tr>`).join("") || `<tr><td colspan="5">${emptyState("暂无运行记录", "新版本处理消息后，从这里开始显示。")}</td></tr>`}</tbody></table></div></article>`;
    } else if (tab === "traces") {
      const rows = (await api("/api/dashboard/traces?hours=24&limit=60")).data.items || [];
      body += `<article class="data-panel"><div class="panel-title"><h3>消息 Trace</h3><p>这是程序运行证据，不包含模型内部思考或完整提示词。</p></div><div class="trace-list">${rows.map((row) => `<details class="trace-row"><summary><b>${escapeHtml(row.scope)}</b> · ${escapeHtml(formatTime(row.created_at))} · ${statusBadge(row.outcome)} <span>${row.duration_ms ?? "—"} ms</span></summary><ol>${row.steps.map((step) => `<li><b>${escapeHtml(step.stage)}</b> <span>${escapeHtml(JSON.stringify(Object.fromEntries(Object.entries(step).filter(([key]) => !["stage", "at_ms"].includes(key)))))} </span></li>`).join("")}</ol></details>`).join("") || emptyState("暂无消息 Trace", "Trace 从当前版本启动后开始采集。")}</div></article>`;
    } else {
      const graph = (await api("/api/dashboard/wiki?limit=200")).data;
      body += `<article class="data-panel"><div class="panel-title"><h3>Wiki 实体关联</h3><p>${graph.available ? "只显示 Markdown 发布页的显式链接，不用相似度伪造边。" : "Wiki 读取器尚未装配，图谱保持未启用。"}</p></div>${graph.nodes?.length ? `<div class="wiki-node-list">${graph.nodes.map((node) => `<button class="memory-tab" data-wiki-id="${escapeHtml(node.id)}">${escapeHtml(node.title)} <small>${escapeHtml(node.kind)}</small></button>`).join("")}</div><p class="muted">${graph.nodes.length} 个实体 · ${graph.links.length} 条链接 · ${graph.dangling_links || 0} 条链接目标不在当前视图</p>` : emptyState(graph.available ? "暂无 Wiki 实体" : "Wiki 未启用", "装配 Wiki 读取器并发布 Markdown 后，这里会显示真实实体链接。")}</article>`;
    }
    content.innerHTML = `<section class="memory-workspace"><div class="memory-toolbar"><div><h2>运行看板</h2><p class="muted">实际程序记录 · 缺失指标保持未知</p></div></div><div class="memory-tabs">${tabsHtml}</div><div class="memory-body">${body}</div></section>`;
    document.querySelectorAll("[data-dashboard-tab]").forEach((button) => { button.onclick = () => renderDashboard(button.dataset.dashboardTab); });
  } catch (error) { content.innerHTML = emptyState("运行数据暂不可用", error.message); }
}


function databaseResultTable(result) {
  if (!result || !result.columns?.length) return emptyState("没有查询结果", "SELECT 没有返回列或记录。");
  const header = result.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
  const rows = result.rows.map((row) => `<tr>${row.map((value) => `<td class="mono">${escapeHtml(value === null ? "NULL" : String(value))}</td>`).join("")}</tr>`).join("");
  return `<div class="table-scroll"><table><thead><tr>${header}</tr></thead><tbody>${rows || `<tr><td colspan="${result.columns.length}">没有记录</td></tr>`}</tbody></table></div><p class="muted database-count">${result.total === undefined ? `${result.rows.length} 行` : `共 ${result.total} 行`}${result.truncated ? " · 已按上限截断" : ""}</p>`;
}

async function renderDatabase(table = "") {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "database"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden"); history.replaceState(null, "", "#database");
  const content = $("#content"); content.innerHTML = `<section class="memory-workspace loading-state">正在读取 SQLite 结构…</section>`;
  try {
    const schema = (await api("/api/database/schema")).data;
    const selected = table || schema.tables?.[0]?.name || "";
    const selectedMeta = schema.tables?.find((item) => item.name === selected);
    const tabs = (schema.tables || []).map((item) => `<button class="memory-tab ${item.name === selected ? "active" : ""}" data-db-table="${escapeHtml(item.name)}">${escapeHtml(item.name)} <small>${item.row_count}</small></button>`).join("");
    let rows = selected ? (await api(`/api/database/tables/${encodeURIComponent(selected)}?limit=100`)).data : null;
    content.innerHTML = `<section class="memory-workspace"><div class="memory-toolbar"><div><h2>数据存储</h2><p class="muted">真实 SQLite 结构与只读查询；写操作继续通过任务、配置和审核业务界面完成。</p></div><span class="data-badge">${schema.tables?.length || 0} 张表</span></div><div class="memory-tabs">${tabs || `<span class="muted">没有可展示的业务表</span>`}</div><div class="memory-body"><div class="overview-grid"><article class="data-panel"><div class="panel-title"><h3>${escapeHtml(selected || "未选择表")}</h3><p>${selectedMeta ? `${selectedMeta.columns.length} 个字段 · ${selectedMeta.row_count} 行` : "选择一张表查看实际数据"}</p></div>${rows ? databaseResultTable(rows) : emptyState("没有可展示的表", "数据库尚未创建业务表。")}</article><article class="data-panel database-query"><div class="panel-title"><h3>只读 SQL</h3><p>仅允许单条 SELECT，最多返回 200 行。</p></div><form id="database-query-form"><textarea name="sql" rows="7" placeholder="SELECT scope, COUNT(*) AS count FROM messages GROUP BY scope ORDER BY count DESC"></textarea><div class="database-query-actions"><label>上限 <input name="limit" type="number" min="1" max="200" value="100"></label><button class="button primary" type="submit">执行查询</button></div></form><div id="database-query-result"></div></article></div></div></section>`;
    document.querySelectorAll("[data-db-table]").forEach((button) => { button.onclick = () => renderDatabase(button.dataset.dbTable); });
    $("#database-query-form").onsubmit = async (event) => { event.preventDefault(); const form = event.currentTarget; const result = $("#database-query-result"); result.textContent = "查询中…"; try { const payload = Object.fromEntries(new FormData(form)); payload.limit = Number(payload.limit); result.innerHTML = databaseResultTable((await api("/api/database/query", { method: "POST", body: JSON.stringify(payload) })).data); } catch (error) { result.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`; } };
  } catch (error) { content.innerHTML = `<section class="memory-workspace">${emptyState("数据库页面加载失败", error.message || "数据库服务尚未就绪")}</section>`; }
}

/* Live operations monitor ------------------------------------------------ */
/* Polls the projection the process owns. Every number has a named source; a
   source that is not bound renders as 未接入 instead of zero. */
const TURN_PAGE = 12, LOG_PAGE = 20, CALL_PAGE = 15;

let monitorView = { minutes: 15, timer: null, paused: false, level: "", q: "", scope: "", updatedAt: 0, turnOffset: 0, logOffset: 0 };

function stopMonitor() {
  if (monitorView.timer) { clearInterval(monitorView.timer); monitorView.timer = null; }
}

function healthTone(status) {
  return status === "down" ? "down" : status === "degraded" ? "degraded" : "ok";
}

function healthText(status) {
  return status === "down" ? "不可用" : status === "degraded" ? "降级运行" : "运行正常";
}

/* A real chart: axes, gridlines and hover targets. Built as SVG markup because
   the console's CSP forbids inline style attributes. */
function areaChart(points) {
  const width = 640, height = 190, left = 34, right = 10, top = 14, bottom = 26;
  const plotW = width - left - right, plotH = height - top - bottom;
  // A flat series would otherwise sit exactly on the top gridline and read as
  // a full-height block; keep at least one unit of headroom.
  const peak = Math.max(0, ...points.map((point) => point.turns));
  const max = Math.max(2, peak);
  const stepX = plotW / Math.max(1, points.length - 1);
  const x = (index) => left + index * stepX;
  const y = (value) => top + plotH * (1 - value / max);
  const coords = points.map((point, index) => [x(index), y(point.turns)]);
  const line = coords.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
  const area = `M${left},${top + plotH} L${line.split(" ").join(" L")} L${(left + (points.length - 1) * stepX).toFixed(1)},${top + plotH} Z`;
  const grid = [0, 0.5, 1].map((ratio) => {
    const gy = top + plotH * (1 - ratio);
    return `<line x1="${left}" y1="${gy.toFixed(1)}" x2="${width - right}" y2="${gy.toFixed(1)}" stroke="#e6eef8" stroke-width="1"/><text x="${left - 6}" y="${(gy + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="#8496ab">${Math.round(max * ratio)}</text>`;
  }).join("");
  const dots = points.map((point, index) => point.errors
    ? `<circle cx="${x(index).toFixed(1)}" cy="${y(point.turns).toFixed(1)}" r="3" fill="#d33b62"><title>${escapeHtml(formatTime(point.at))} · ${point.turns} 条 · 失败 ${point.errors}</title></circle>`
    : "").join("");
  const label = (index, anchor) => `<text x="${x(index).toFixed(1)}" y="${height - 8}" text-anchor="${anchor}" font-size="9" fill="#8496ab">${escapeHtml((formatTime(points[index].at) || "").slice(11, 16))}</text>`;
  const empty = points.every((point) => !point.turns)
    ? `<text x="${left + plotW / 2}" y="${top + plotH / 2}" text-anchor="middle" font-size="11" fill="#9aabbf">这段时间没有消息</text>`
    : "";
  return `<svg class="area-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="每分钟消息量">
    ${grid}
    <path d="${area}" fill="rgba(23,105,224,.12)" stroke="none"/>
    <polyline points="${line}" fill="none" stroke="#1769e0" stroke-width="1.8" stroke-linejoin="round"/>
    ${dots}${empty}
    ${label(0, "start")}${label(Math.floor((points.length - 1) / 2), "middle")}${label(points.length - 1, "end")}
  </svg>`;
}


/* Paged, never scrolled. A clipped last row inside a scroll box hides how much
   data there is and is the single ugliest thing a table can do. */
function pager(data, key) {
  const pages = data.pages || 1;
  if (!data.total) return "";
  const offset = data.offset || 0;
  const limit = Math.max(1, data.limit || 1);
  const page = Math.floor(offset / limit) + 1;
  const from = offset + 1;
  const to = Math.min(data.total, offset + (data.items || []).length);
  return `<div class="pager"><span class="muted small">${from}–${to} / 共 ${data.total} 条</span><span class="pager-gap"></span><button class="button subtle mini" type="button" data-pager="${key}" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button><span class="small pager-count">${page} / ${pages}</span><button class="button subtle mini" type="button" data-pager="${key}" data-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button></div>`;
}

function bindPager(reset) {
  document.querySelectorAll("[data-pager]").forEach((button) => {
    button.onclick = () => {
      const page = Number(button.dataset.page);
      if (!Number.isFinite(page) || page < 1) return;
      reset(button.dataset.pager, (page - 1) * (button.dataset.pager === "turns" ? TURN_PAGE : button.dataset.pager === "logs" ? LOG_PAGE : CALL_PAGE));
    };
  });
}

function turnRows(turns) {
  if (!turns.items.length) return `<tr><td colspan="5">${emptyState("没有消息记录", "最近 24 小时这个进程没有处理过消息。")}</td></tr>`;
  return turns.items.map((row) => {
    const tools = row.tools.length ? row.tools.slice(0, 3).map((tool) => `<span class="trig">${escapeHtml(tool)}</span>`).join("") : `<span class="muted">—</span>`;
    return `<tr><td class="mono">${escapeHtml(formatTime(row.at))}</td><td>${escapeHtml(row.scope)}</td><td>${statusBadge(row.outcome)}</td><td>${row.duration_ms == null ? "—" : `${row.duration_ms} ms`}</td><td><span class="trig-row">${tools}</span></td></tr>`;
  }).join("");
}

function scopeRows(scopes) {
  if (!scopes.items.length) return `<tr><td colspan="4">${emptyState("没有会话数据", "Trace 里还没有会话范围。")}</td></tr>`;
  const max = Math.max(1, ...scopes.items.map((row) => row.turns));
  return scopes.items.map((row) => `<tr><td>${escapeHtml(row.scope)}</td><td class="num">${row.turns}</td><td class="num">${row.errors}</td><td><span class="bar-track"><span class="bar-fill w${Math.max(1, Math.round((row.turns / max) * 10))}${row.errors ? " bad" : ""}"></span></span></td></tr>`).join("");
}


/* Views animate in once, on mount. The live board polls every five seconds;
   replaying the entrance animation on each poll is what made it flash. */
const mountedViews = new Set();

function mountView(content, key) {
  if (mountedViews.has(key)) return;
  mountedViews.add(key);
  content.firstElementChild?.classList.add("entering");
}

function silentText(seconds) {
  if (seconds == null) return "无记录";
  return seconds < 120 ? `${seconds} 秒前` : `${Math.round(seconds / 60)} 分钟前`;
}

function uptimeText(seconds) {
  if (seconds == null) return "未知";
  return seconds > 3600 ? `${(seconds / 3600).toFixed(1)} 小时` : `${Math.round(seconds / 60)} 分钟`;
}

/* Only the clock fields are rewritten on a poll; everything else keeps its DOM
   node so the board never flashes and the scroll position survives. */
function updateClocks(live) {
  document.querySelectorAll("[data-clock]").forEach((node) => {
    const kind = node.dataset.clock;
    if (kind === "uptime") node.textContent = uptimeText(live.uptime_seconds);
    else if (kind === "silent") node.textContent = silentText(live.silent_seconds);
    else if (kind === "updated") node.textContent = `最后更新 ${formatTime(live.now).slice(11)}`;
  });
}

function monitorSignature(live, alerts, series, logs, turns, scopes) {
  const { now, uptime_seconds, silent_seconds, last_turn_at, ...structural } = live;
  return JSON.stringify([structural, alerts.items, series.points, logs.items, logs.total, turns.items, turns.total, turns.offset, scopes.items]);
}

function monitorShell(live, alerts, series, logs, turns, scopes) {
  const onebot = live.onebot;
  const scheduler = live.scheduler;
  const errorRate = live.error_ratio == null ? "未知" : `${(live.error_ratio * 100).toFixed(1)}%`;
  const total = series.points.reduce((sum, point) => sum + point.turns, 0);
  const failed = series.points.reduce((sum, point) => sum + point.errors, 0);
  const kpis = `<div class="stat-strip">
    <div><span>窗口消息</span><b>${live.turns}</b><small>最近 ${live.window_minutes} 分钟</small></div>
    <div><span>已回复</span><b>${live.replies}</b><small>失败 ${live.errors} 轮</small></div>
    <div><span>失败率</span><b>${errorRate}</b><small>${live.errors}/${live.turns}</small></div>
    <div><span>端到端 P95</span><b>${live.latency_p95_ms == null ? "未知" : `${live.latency_p95_ms} ms`}</b><small>窗口内已完成轮次</small></div>
    <div><span>最后一条消息</span><b data-clock="silent">${silentText(live.silent_seconds)}</b><small>${live.last_turn_at ? formatTime(live.last_turn_at) : "还没有 Trace"}</small></div>
    <div><span>进程运行</span><b data-clock="uptime">${uptimeText(live.uptime_seconds)}</b><small>模型 ${escapeHtml(live.model.name || "未配置")}</small></div>
  </div>`;
  const inbound = onebot ? `<dl class="fact-grid compact">
      <div><dt>连接</dt><dd>${onebot.connected ? "已连接" : "未连接"}</dd></div>
      <div><dt>入站接受</dt><dd>${onebot.accepted}</dd></div>
      <div><dt>已处理</dt><dd>${onebot.completed}</dd></div>
      <div><dt>处理失败</dt><dd>${onebot.failed}</dd></div>
      <div><dt>丢弃帧</dt><dd>${onebot.dropped}</dd></div>
      <div><dt>并发通道</dt><dd>${onebot.lanes == null ? "未知" : onebot.lanes}</dd></div>
      <div><dt>在途请求</dt><dd>${onebot.pending}</dd></div>
    </dl>` : `<p class="muted">OneBot 网关未接入：当前控制台没有随 Bot 运行，连接与入站计数无法读取。</p>`;
  const scheduled = scheduler ? `<dl class="fact-grid compact">
      <div><dt>进行中</dt><dd>${scheduler.running}</dd></div>
      <div><dt>近 7 日失败</dt><dd>${scheduler.failed_7d}</dd></div>
      <div><dt>采样记录</dt><dd>${scheduler.runs_sampled}</dd></div>
    </dl>` : `<p class="muted">调度器未接入。</p>`;
  const alertRows = alerts.items.length ? alerts.items.map((item) => `<li class="alert-row ${escapeHtml(item.level)}"><span>${escapeHtml(item.level)}</span><div><b>${escapeHtml(item.title)}</b><small>${escapeHtml(item.detail)}</small></div></li>`).join("") : `<li class="muted">没有触发告警规则。</li>`;
  const levelChip = (id, label) => `<button class="chip ${monitorView.level === id ? "active" : ""}" data-log-level="${id}">${label}</button>`;
  const logRows = logs.available && logs.items.length
    ? logs.items.map((item) => `<tr><td class="mono">${escapeHtml(formatTime(item.at))}</td><td><span class="data-badge log-${escapeHtml(String(item.level).toLowerCase())}">${escapeHtml(item.level)}</span></td><td class="mono">${escapeHtml(item.logger)}</td><td>${escapeHtml(item.message)}</td></tr>`).join("")
    : `<tr><td colspan="4">${emptyState(logs.available ? "没有匹配的日志" : "日志未接入", logs.available ? "换个级别或关键字再试。" : "随 Bot 启动控制台后会显示进程日志。")}</td></tr>`;
  return `<section class="memory-workspace" data-monitor-root>
    <div class="memory-toolbar"><div><h2>线上监控</h2><p class="muted">连接计数来自网关，消息指标来自 Trace，日志来自进程环形缓冲。${monitorView.paused ? "已暂停刷新。" : "每 5 秒刷新。"}</p></div>
      <div class="monitor-actions">
        <span class="live-dot${monitorView.paused ? " paused" : ""}"></span><span class="muted">${monitorView.paused ? "已暂停" : "实时"}</span>
        <select id="monitor-window"><option value="5" ${monitorView.minutes === 5 ? "selected" : ""}>5 分钟</option><option value="15" ${monitorView.minutes === 15 ? "selected" : ""}>15 分钟</option><option value="60" ${monitorView.minutes === 60 ? "selected" : ""}>60 分钟</option></select>
        <button id="monitor-pause" class="button subtle" type="button">${monitorView.paused ? "继续" : "暂停"}</button>
        <button id="monitor-refresh" class="button subtle" type="button">刷新</button>
      </div></div>
    <div class="health-banner ${healthTone(live.health.status)}"><div><b>${healthText(live.health.status)}</b><p>${live.health.reasons.map(escapeHtml).join("；")}</p></div><span class="data-badge" data-clock="updated">最后更新 ${escapeHtml(formatTime(live.now).slice(11))}</span></div>
    ${kpis}
    <div class="overview-grid wide-left">
      <article class="data-panel"><div class="panel-title"><h3>吞吐（最近 60 分钟）</h3><p>每分钟消息数，红点是该分钟出现过失败轮次。</p></div>${areaChart(series.points)}<p class="muted small">合计 ${total} 条 · 失败 ${failed} 条 · 峰值 ${Math.max(0, ...series.points.map((point) => point.turns))} 条/分钟</p></article>
      <article class="data-panel"><div class="panel-title"><h3>会话分布（24 小时）</h3><p>哪个群或私聊通道在占用处理能力。</p></div><div class="table-scroll"><table class="tight"><thead><tr><th>会话</th><th class="num">轮次</th><th class="num">失败</th><th>占比</th></tr></thead><tbody>${scopeRows(scopes)}</tbody></table></div></article>
    </div>
    <div class="overview-grid">
      <article class="data-panel"><div class="panel-title"><h3>告警</h3><p>规则命中即显示，不做静默聚合。</p></div><ul class="alert-list">${alertRows}</ul></article>
      <article class="data-panel"><div class="panel-title"><h3>OneBot 网关与调度</h3><p>掉线时入站消息全部丢失，这是最优先的告警。</p></div>${inbound}${scheduled}</article>
    </div>
    <article class="data-panel"><div class="panel-title"><h3>最近消息</h3><p>最新 12 轮，含每轮真正执行过的工具。点击会话可只看该会话。</p></div>
      ${monitorView.scope ? `<div class="filter-row"><button class="chip active" id="monitor-clear-scope">只看 ${escapeHtml(monitorView.scope)} ✕</button></div>` : ""}
      <div class="table-scroll"><table class="tight"><thead><tr><th>时间</th><th>会话</th><th>结果</th><th>耗时</th><th>工具</th></tr></thead><tbody id="monitor-turns">${turnRows(turns)}</tbody></table></div>${pager(turns, "turns")}</article>
    <article class="data-panel"><div class="panel-title"><h3>进程日志</h3><p>最多 500 条环形缓冲，不写入磁盘。${logs.available ? `当前缓冲里有 ${logs.levels.join(" / ") || "0"} 级别。` : ""}</p></div>
      <div class="filter-row">${levelChip("", "全部")}${logs.levels.map((level) => levelChip(level, level)).join("")}<input id="monitor-log-q" type="search" placeholder="按内容或来源过滤…" value="${escapeHtml(monitorView.q)}"></div>
      <div class="table-scroll"><table class="tight"><thead><tr><th>时间</th><th>级别</th><th>来源</th><th>内容</th></tr></thead><tbody>${logRows}</tbody></table></div>${pager(logs, "logs")}</article>
  </section>`;
}

async function renderMonitor() {
  stopMonitor();
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "monitor"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden");
  history.replaceState(null, "", "#monitor");
  const content = $("#content");
  content.innerHTML = `<section class="memory-workspace loading-state">正在读取运行状态…</section>`;
  monitorView.signature = "";
  const paint = async () => {
    try {
      const scope = monitorView.scope ? `&scope=${encodeURIComponent(monitorView.scope)}` : "";
      const [live, alerts, series, logs, turns, scopes] = await Promise.all([
        api(`/api/monitor/live?minutes=${monitorView.minutes}`),
        api(`/api/monitor/alerts?minutes=${monitorView.minutes}`),
        api("/api/monitor/series?minutes=60"),
        api(`/api/monitor/logs?limit=${LOG_PAGE}&offset=${monitorView.logOffset}&level=${encodeURIComponent(monitorView.level)}&q=${encodeURIComponent(monitorView.q)}`),
        api(`/api/monitor/turns?limit=${TURN_PAGE}&offset=${monitorView.turnOffset}${scope}`),
        api("/api/monitor/scopes?hours=24"),
      ]);
      monitorView.updatedAt = Date.now();
      const signature = monitorSignature(live.data, alerts.data, series.data, logs.data, turns.data, scopes.data);
      if (signature === monitorView.signature) {
        // Nothing structural changed — refresh the clocks and leave the DOM
        // alone. Replacing it every five seconds is what caused the flashing.
        updateClocks(live.data);
        return;
      }
      monitorView.signature = signature;
      const scrollY = window.scrollY;
      const focused = document.activeElement;
      const focusId = focused && focused.id ? focused.id : "";
      const caret = focused && typeof focused.selectionStart === "number"
        ? [focused.selectionStart, focused.selectionEnd] : null;
      content.innerHTML = monitorShell(live.data, alerts.data, series.data, logs.data, turns.data, scopes.data);
      mountView(content, "monitor");
      window.scrollTo({ top: scrollY });
      if (focusId) {
        const restored = document.getElementById(focusId);
        if (restored) {
          restored.focus({ preventScroll: true });
          if (caret && typeof restored.setSelectionRange === "function") {
            try { restored.setSelectionRange(caret[0], caret[1]); } catch { /* not a text field */ }
          }
        }
      }
      const windowSelect = $("#monitor-window");
      if (windowSelect) windowSelect.onchange = () => { monitorView.minutes = Number(windowSelect.value); monitorView.turnOffset = 0; paint(); };
      const refresh = $("#monitor-refresh");
      if (refresh) refresh.onclick = () => paint();
      const pause = $("#monitor-pause");
      if (pause) pause.onclick = () => {
        monitorView.paused = !monitorView.paused;
        if (monitorView.paused) stopMonitor();
        else startPolling(paint);
        paint();
      };
      document.querySelectorAll("[data-log-level]").forEach((button) => {
        button.onclick = () => { monitorView.level = button.dataset.logLevel; monitorView.logOffset = 0; paint(); };
      });
      const logQuery = $("#monitor-log-q");
      if (logQuery) {
        logQuery.onkeydown = (event) => { if (event.key === "Enter") { monitorView.q = event.currentTarget.value.trim(); monitorView.logOffset = 0; paint(); } };
      }
      const clearScope = $("#monitor-clear-scope");
      if (clearScope) clearScope.onclick = () => { monitorView.scope = ""; monitorView.turnOffset = 0; paint(); };
      bindPager((key, offset) => {
        if (key === "turns") monitorView.turnOffset = offset;
        else monitorView.logOffset = offset;
        paint();
      });
      document.querySelectorAll("#monitor-turns tr").forEach((row) => {
        const cell = row.children[1];
        if (!cell) return;
        const scopeName = cell.textContent.trim();
        cell.onclick = () => { monitorView.scope = scopeName; monitorView.turnOffset = 0; paint(); };
        cell.classList.add("clickable");
      });
    } catch (error) {
      content.innerHTML = `<section class="memory-workspace">${emptyState("监控不可用", error.message || "服务尚未就绪")}</section>`;
    }
  };
  await paint();
  if (!monitorView.paused) startPolling(paint);
}

function startPolling(paint) {
  stopMonitor();
  monitorView.timer = setInterval(() => {
    if (!document.querySelector("[data-monitor-root]")) { stopMonitor(); return; }
    paint();
  }, 5000);
}

/* Tool call console ------------------------------------------------------ */
const OUTCOME_LABELS = { completed: "完成", sent: "已发送", error: "失败", failed: "失败", timeout: "超时", running: "进行中", unknown: "未知" };

function outcomeLabel(name) {
  return OUTCOME_LABELS[name] || name;
}

let toolsView = { hours: 24, tool: "", offset: 0 };

function toolRows(rows) {
  if (!rows.length) return `<tr><td colspan="6">${emptyState("没有工具调用", "当前时间范围里模型没有请求任何工具。")}</td></tr>`;
  return rows.map((row) => `<tr class="tool-row" data-tool-name="${escapeHtml(row.tool)}"><td><b>${escapeHtml(row.tool)}</b></td><td>${row.calls}</td><td>${row.completed}</td><td>${row.failed + row.unknown}</td><td>${row.success_rate == null ? "—" : `${(row.success_rate * 100).toFixed(0)}%`}</td><td>${row.last_at ? escapeHtml(formatTime(row.last_at)) : "—"}</td></tr>`).join("");
}

async function renderTools() {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "tools"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden");
  history.replaceState(null, "", "#tools");
  const content = $("#content");
  content.innerHTML = `<section class="memory-workspace loading-state">正在读取工具调用记录…</section>`;
  try {
    const [overview, calls] = await Promise.all([
      api(`/api/tools/overview?hours=${toolsView.hours}${toolsView.tool ? `&tool=${encodeURIComponent(toolsView.tool)}` : ""}`),
      api(`/api/tools/calls?hours=${toolsView.hours}&limit=${CALL_PAGE}&offset=${toolsView.offset}${toolsView.tool ? `&tool=${encodeURIComponent(toolsView.tool)}` : ""}`),
    ]);
    const data = overview.data;
    const success = data.tools.reduce((sum, row) => sum + row.completed, 0);
    const failed = data.tools.reduce((sum, row) => sum + row.failed + row.unknown, 0);
    const callRows = calls.data.items.length ? calls.data.items.map((item) => `<tr><td>${escapeHtml(formatTime(item.at))}</td><td class="mono">${escapeHtml(item.scope)}</td><td><b>${escapeHtml(item.tool)}</b></td><td>${statusBadge(item.outcome)}</td><td>${escapeHtml(item.error_kind || "—")}</td><td>${item.duration_ms == null ? "未记录" : `${item.duration_ms} ms`}</td></tr>`).join("") : `<tr><td colspan="6">${emptyState("没有调用明细", "工具调用会随消息 Trace 一起记录。")}</td></tr>`;
    content.innerHTML = `<section class="memory-workspace">
      <div class="memory-toolbar"><div><h2>工具调用</h2><p class="muted">一次调用写入 running 与终态两条 Trace，这里按终态计数，不会翻倍。</p></div>
        <div class="monitor-actions">${toolsView.tool ? `<button id="tool-clear" class="button subtle" type="button">清除筛选：${escapeHtml(toolsView.tool)}</button>` : ""}<select id="tool-hours"><option value="1" ${toolsView.hours === 1 ? "selected" : ""}>最近 1 小时</option><option value="24" ${toolsView.hours === 24 ? "selected" : ""}>最近 24 小时</option><option value="168" ${toolsView.hours === 168 ? "selected" : ""}>最近 7 天</option></select></div></div>
      <div class="metric-grid"><article class="metric-card"><span>调用次数</span><strong>${data.total_calls}</strong><small>${data.traces_scanned} 条 Trace 内</small></article><article class="metric-card"><span>成功</span><strong>${success}</strong><small>终态 completed</small></article><article class="metric-card"><span>失败/未知</span><strong>${failed}</strong><small>包含异常结束</small></article><article class="metric-card"><span>在途未结束</span><strong>${data.inflight}</strong><small>只有 running 没有终态</small></article></div>
      <div class="overview-grid">
        <article class="data-panel"><div class="panel-title"><h3>按工具汇总</h3><p>点击一行只看该工具的调用明细。${data.truncated ? "已按扫描上限截断。" : ""}</p></div><div class="table-scroll"><table><thead><tr><th>工具</th><th>次数</th><th>成功</th><th>失败</th><th>成功率</th><th>最近一次</th></tr></thead><tbody>${toolRows(data.tools)}</tbody></table></div></article>
        <article class="data-panel"><div class="panel-title"><h3>结果分布</h3><p>没有终态的工具调用不计入成功。</p></div><ul class="frequency-list">${Object.entries(data.outcomes).map(([name, count]) => `<li><span>${escapeHtml(outcomeLabel(name))}</span><strong>${count} 次</strong></li>`).join("") || `<li class="muted">当前时间范围没有工具调用</li>`}</ul></article>
      </div>
      <article class="data-panel"><div class="panel-title"><h3>最近调用明细</h3><p>工具步本身不记录耗时，因此这里显示“未记录”而不是借用整轮耗时。</p></div><div class="table-scroll"><table><thead><tr><th>时间</th><th>会话</th><th>工具</th><th>结果</th><th>错误类型</th><th>工具耗时</th></tr></thead><tbody>${callRows}</tbody></table></div>${pager(calls.data, "calls")}</article>
    </section>`;
    $("#tool-hours").onchange = (event) => { toolsView.hours = Number(event.currentTarget.value); toolsView.offset = 0; renderTools(); };
    document.querySelectorAll("[data-tool-name]").forEach((row) => { row.onclick = () => { toolsView.tool = row.dataset.toolName; toolsView.offset = 0; renderTools(); }; });
    const clear = $("#tool-clear");
    if (clear) clear.onclick = () => { toolsView.tool = ""; toolsView.offset = 0; renderTools(); };
    bindPager((key, offset) => { toolsView.offset = offset; renderTools(); });
    mountView(content, "tools");
  } catch (error) { content.innerHTML = `<section class="memory-workspace">${emptyState("工具调用不可用", error.message || "请随 Bot 启动控制台")}</section>`; }
}

/* Routing board ---------------------------------------------------------- */
async function applyRoute(changes, message) {
  try {
    await api("/api/routes", { method: "PUT", body: JSON.stringify({ changes }) });
    toast(`${message}，重启 Bot 后生效`);
    renderRouting();
  } catch (error) { toast(error.message || "保存失败", true); }
}

async function renderRouting() {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "routing"));
  $(".search").classList.add("hidden"); $("#save").classList.add("hidden");
  history.replaceState(null, "", "#routing");
  const content = $("#content");
  content.innerHTML = `<section class="memory-workspace loading-state">正在读取链路配置…</section>`;
  try {
    const data = (await api("/api/routes/overview")).data;
    const graph = data.graph;
    const pipeline = data.pipeline.map((stage) => `<li class="${stage.enabled ? "on" : "off"}"><span>${escapeHtml(stage.label)}</span><small>${escapeHtml(stage.detail)}</small></li>`).join("");
    const links = data.llm.map((link) => `<article class="link-card"><div class="record-meta"><b>${escapeHtml(link.label)}</b><span class="data-badge ${link.configured ? "status-active" : ""}">${link.configured ? "已配置" : "未配置"}</span></div><h3>${escapeHtml(link.model || "—")}</h3><p class="mono">${escapeHtml(link.base_url || "未单独配置地址")}</p><small>${escapeHtml(link.note)}</small></article>`).join("");
    const modes = graph.modes.map((mode) => `<button class="mode-button ${graph.mode === mode.id ? "active" : ""}" data-mode="${escapeHtml(mode.id)}"><b>${escapeHtml(mode.id)}</b><small>${escapeHtml(mode.label)}</small></button>`).join("");
    const artifacts = data.artifacts.items.length ? data.artifacts.items.map((item) => {
      const manifest = item.manifest || {};
      const current = item.path === graph.snapshot;
      return `<li class="${current ? "current" : ""}"><div><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.path)} · ${escapeHtml(formatTime(item.modified_at))}</small><p class="muted">${manifest.status ? `状态 ${escapeHtml(String(manifest.status))}` : "无 manifest"}${manifest.proposals == null ? "" : ` · 样本 ${manifest.proposals}`}${manifest.missing_cases == null ? "" : ` · 缺失 ${manifest.missing_cases}`}</p></div><button class="button subtle" data-artifact="${escapeHtml(item.path)}" ${current ? "disabled" : ""}>${current ? "当前使用" : "设为当前"}</button></li>`;
    }).join("") : `<li class="muted">${data.artifacts.exists ? "派生目录里没有可用的图谱产物。" : "没有 data/private/derived 目录。"}</li>`;
    content.innerHTML = `<section class="memory-workspace">
      <div class="memory-toolbar"><div><h2>链路与图谱</h2><p class="muted">这里写的是 Bot 启动时读取的 .env，保存后必须重启才生效。</p></div><span class="data-badge">${graph.effective === "off" ? "图谱路由关闭" : `图谱路由 ${escapeHtml(graph.effective)}`}</span></div>
      <article class="data-panel"><div class="panel-title"><h3>一轮消息的链路</h3><p>顺序与运行时一致，灰色表示该环节当前关闭。</p></div><ol class="pipeline">${pipeline}</ol></article>
      <div class="overview-grid">
        <article class="data-panel"><div class="panel-title"><h3>模型链路</h3><p>每条链路的模型独立配置，未配置的环节会跳过。</p></div><div class="link-grid">${links}</div></article>
        <article class="data-panel"><div class="panel-title"><h3>行为路由</h3><p>${escapeHtml(graph.reason)}</p></div><div class="mode-row">${modes}</div><label class="field-label" for="graph-path">图谱快照路径</label><input id="graph-path" value="${escapeHtml(graph.snapshot)}" placeholder="data/private/derived/.../graph.json"><label class="field-label" for="calibration-path">校准文件（可选）</label><input id="calibration-path" value="${escapeHtml(graph.calibration)}" placeholder="留空表示不做先验校准"><div class="route-actions"><button id="route-save" class="button primary" type="button">保存路径</button><button id="route-probe" class="button subtle" type="button">验证快照可用性</button></div><p id="route-probe-result" class="muted"></p>
          <div class="switch-row"><span>记忆实体图谱</span><label class="switch"><input id="memory-graph" type="checkbox" ${data.memory_graph ? "checked" : ""}><span></span></label></div>
          <div class="switch-row"><span>LM Wiki（蒸馏产物）</span><label class="switch"><input id="wiki-switch" type="checkbox" ${data.wiki.enabled ? "checked" : ""}><span></span></label></div>
          <p class="muted">Wiki 目录 ${escapeHtml(data.wiki.root || "未配置")} · ${data.wiki.root_exists ? "目录存在" : "目录不存在"}</p>
        </article>
      </div>
      <article class="data-panel"><div class="panel-title"><h3>可用图谱产物</h3><p>来自 ${escapeHtml(data.artifacts.root)}，按修改时间倒序。</p></div><ul class="artifact-list">${artifacts}</ul></article>
    </section>`;
    document.querySelectorAll("[data-mode]").forEach((button) => { button.onclick = () => applyRoute({ BOT_BEHAVIOR_MODE: button.dataset.mode }, `行为路由已切换为 ${button.dataset.mode}`); });
    document.querySelectorAll("[data-artifact]").forEach((button) => { button.onclick = () => applyRoute({ BOT_BEHAVIOR_GRAPH_PATH: button.dataset.artifact }, "图谱快照已更新"); });
    $("#route-save").onclick = () => applyRoute({ BOT_BEHAVIOR_GRAPH_PATH: $("#graph-path").value.trim(), BOT_BEHAVIOR_CALIBRATION_PATH: $("#calibration-path").value.trim() }, "图谱路径已保存");
    $("#route-probe").onclick = async () => {
      const target = $("#route-probe-result");
      target.textContent = "正在按运行时的方式加载快照…";
      try { const result = (await api("/api/routes/probe", { method: "POST", body: "{}" })).data; target.textContent = result.detail; target.className = result.ok ? "probe-ok" : "probe-bad"; }
      catch (error) { target.textContent = error.message; target.className = "probe-bad"; }
    };
    $("#memory-graph").onchange = (event) => applyRoute({ BOT_MEMORY_GRAPH_ENABLED: event.currentTarget.checked ? "true" : "false" }, "记忆图谱开关已保存");
    $("#wiki-switch").onchange = (event) => applyRoute({ BOT_WIKI_ENABLED: event.currentTarget.checked ? "true" : "false" }, "Wiki 开关已保存");
    mountView(content, "routing");
  } catch (error) { content.innerHTML = `<section class="memory-workspace">${emptyState("链路配置不可用", error.message || "读取 .env 失败")}</section>`; }
}

function activateSection(id) {
  stopMonitor();
  if (id === "monitor") {
    renderMonitor();
    $("#search").value = "";
    return;
  }
  if (id === "tools") {
    renderTools();
    $("#search").value = "";
    return;
  }
  if (id === "routing") {
    renderRouting();
    $("#search").value = "";
    return;
  }
  if (id === "wiki-admin") {
    renderWikiAdmin();
    $("#search").value = "";
    return;
  }
  if (id === "skills") {
    renderSkills();
    $("#search").value = "";
    return;
  }
  if (id === "database") {
    renderDatabase();
    $("#search").value = "";
    return;
  }
  if (id === "dashboard") {
    renderDashboard();
    $("#search").value = "";
    return;
  }
  if (id === "memory") {
    renderMemory();
    $("#search").value = "";
    return;
  }
  if (id === "experiments") {
    renderExperiments();
    $("#search").value = "";
    return;
  }
  if (id === "guide") {
    renderGuide();
    $("#search").value = "";
    return;
  }
  if (id === "schedules") {
    renderSchedules();
    $("#search").value = "";
    return;
  }

  $(".search").classList.remove("hidden");
  $("#save").classList.remove("hidden");
  const section = state.sections.find((item) => item.id === id) || state.sections[0];
  const settings = state.settings.filter((setting) => setting.section === section.id);
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.target === section.id);
  });
  $("#content").innerHTML = `<article class="section-card active-section"><div class="section-head compact-head"><div><h3>${escapeHtml(section.label)}</h3><p>${escapeHtml(section.description)}</p></div><span class="section-count">${settings.length} 项参数</span></div><div class="settings-grid">${settings.map((setting) => control(setting, state.values[setting.key])).join("")}</div></article>`;
  $("#search").value = "";
  bindControls();
  history.replaceState(null, "", `#${section.id}`);
}

function render(data) {
  state = data;
  original = {};
  Object.entries(data.values).forEach(([key, value]) => { original[key] = value.value; });
  const modules = data.sections.map((section) => `<button class="nav-item" data-target="${section.id}"><span class="nav-glyph">${glyphs[section.id] || "·"}</span><span>${escapeHtml(section.label)}</span></button>`).join("");
  $("#nav").innerHTML = `${modules}<div class="nav-divider"></div><button class="nav-item" data-target="monitor"><span class="nav-glyph">${glyphs.monitor}</span><span>线上监控</span></button><button class="nav-item" data-target="tools"><span class="nav-glyph">${glyphs.tools}</span><span>工具调用</span></button><button class="nav-item" data-target="dashboard"><span class="nav-glyph">${glyphs.dashboard}</span><span>运行看板</span></button><button class="nav-item" data-target="skills"><span class="nav-glyph">${glyphs.skills}</span><span>Skill 管理</span></button><button class="nav-item" data-target="wiki-admin"><span class="nav-glyph">${glyphs.wikiadmin}</span><span>Wiki 管理</span></button><button class="nav-item" data-target="database"><span class="nav-glyph">${glyphs.database}</span><span>数据存储</span></button><button class="nav-item" data-target="routing"><span class="nav-glyph">${glyphs.routing}</span><span>链路与图谱</span></button><div class="nav-divider"></div><button class="nav-item" data-target="memory"><span class="nav-glyph">${glyphs.memory}</span><span>记忆中心</span></button><button class="nav-item" data-target="schedules"><span class="nav-glyph">${glyphs.schedules}</span><span>任务中心</span></button><button class="nav-item" data-target="experiments"><span class="nav-glyph">${glyphs.experiments}</span><span>实验看板</span></button><button class="nav-item" data-target="guide"><span class="nav-glyph">${glyphs.guide}</span><span>使用指南</span></button>`;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.onclick = () => activateSection(button.dataset.target);
  });
  activateSection(location.hash.slice(1) || data.sections[0].id);
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    $("#restart-banner").classList.toggle("hidden", !status.restart_required);
  } catch (_) {
    // The next periodic refresh can recover without disturbing the form.
  }
}

async function enter() {
  render(await api("/api/config"));
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  await refreshStatus();
  setInterval(refreshStatus, 30000);
}

$("#login-form").onsubmit = async (event) => {
  event.preventDefault();
  const nextToken = $("#token").value;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: nextToken }),
    });
    if (!response.ok) throw new Error((await response.json()).error);
    token = nextToken;
    sessionStorage.setItem("kinna-bot-token", token);
    $("#login-error").textContent = "";
    await enter();
  } catch (error) {
    $("#login-error").textContent = error.message || "登录失败";
  }
};

$("#save").onclick = async () => {
  const button = $("#save");
  const changes = {};
  document.querySelectorAll("[data-key]").forEach((element) => {
    const value = element.type === "checkbox" ? (element.checked ? "true" : "false") : element.value;
    const metadata = state.settings.find((setting) => setting.key === element.dataset.key);
    if (!(metadata.secret && value === "") && value !== original[element.dataset.key]) {
      changes[element.dataset.key] = value;
    }
  });
  if (!Object.keys(changes).length) { toast("没有需要保存的更改"); return; }

  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const result = await api("/api/config", { method: "PUT", body: JSON.stringify({ changes }) });
    Object.entries(changes).forEach(([key, value]) => { original[key] = value; });
    $("#restart-banner").classList.remove("hidden");
    toast(`已保存 ${result.updated.length} 项配置`);
  } catch (error) {
    toast(error.message || "保存失败", true);
  } finally {
    button.disabled = false;
    button.textContent = "保存更改";
  }
};

$("#search").oninput = (event) => {
  const query = event.target.value.trim().toLowerCase();
  document.querySelectorAll(".control").forEach((element) => {
    element.classList.toggle("hidden", query && !element.dataset.search.includes(query));
  });
};

if (token) {
  enter().catch(() => { sessionStorage.removeItem("kinna-bot-token"); token = ""; });
}

$("#detail-close").onclick = () => $("#detail-dialog").close();
$("#detail-dialog").onclick = (event) => {
  if (event.target === $("#detail-dialog")) $("#detail-dialog").close();
};
