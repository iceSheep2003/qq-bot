const $ = (selector) => document.querySelector(selector);

let token = sessionStorage.getItem("kinna-bot-token") || "";
let state = null;
let original = {};

const glyphs = {
  connection: "⌁", conversation: "◌", personality: "✦", proactive: "◴",
  moderation: "◇", media: "▣", system: "⚙", memory: "◎", schedules: "◷",
  experiments: "⌬", guide: "?", dashboard: "▤",
};

let memoryView = { tab: "overview", scope: "", after: "", previous: [], query: "", status: "" };
let memoryRenderEpoch = 0;
let graphController = null;
let scheduleView = { tab: "jobs", overview: null, jobs: [] };

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
  const labels = { active: "有效", candidate: "候选", accepted_as_report: "已接纳的说法", accepted: "已接纳", disputed: "有争议", withdrawn: "已撤回", observed: "已观察", unknown: "未确认", retired: "已停用", archived: "归档", expired: "过期", superseded: "已替代", succeeded: "成功", failed: "失败", running: "进行中", approved: "已审核", rejected: "已拒绝" };
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
      <div><p class="eyebrow">MEMORY CENTER</p><h2>记忆中心</h2></div>
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
    <div class="schedule-toolbar"><div><p class="eyebrow">TASK CENTER</p><h2>任务中心</h2></div><button id="schedule-create" class="button primary" type="button">新建任务</button></div>
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

function renderScheduleJobs(rows) {
  if (!rows.length) return emptyState("还没有定时任务", "点击右上角新建第一个任务。");
  return `<div class="task-table">${rows.map((job) => `<article class="task-row">
    <div class="task-main"><div class="record-meta">${scheduleStatus(job)}<span>${escapeHtml(job.source === "config" ? "管理员任务" : "建议模板")}</span><span>${escapeHtml(job.action)}</span></div><h3>${escapeHtml(job.name || job.config_key)}</h3><p>${escapeHtml(scheduleLabel(job))} · 群 ${escapeHtml(job.group_id)}</p></div>
    <div class="task-next"><small>下次执行</small><b>${job.enabled ? formatTime(job.next_run) : "—"}</b></div>
    <div class="task-actions">${job.source === "config" ? `<button data-task-run="${job.id}" class="button subtle">立即执行</button><button data-task-edit="${escapeHtml(job.config_key)}" class="button subtle">编辑</button>` : ""}</div>
  </article>`).join("")}</div>`;
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

function openScheduleEditor(job = null) {
  const draft = jobDraft(job || {}), editing = Boolean(job && job.config_key);
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
    const [overview, jobs] = await Promise.all([api("/api/schedules/overview"), api("/api/schedules/jobs")]);
    scheduleView.overview = overview.data; scheduleView.jobs = jobs.data;
    let payload = jobs.data;
    if (tab === "runs") payload = (await api("/api/schedules/runs?limit=200")).data;
    if (tab === "templates") payload = (await api("/api/schedules/suggestions")).data;
    const body = tab === "jobs" ? renderScheduleJobs(payload) : tab === "timeline" ? renderScheduleTimeline(payload) : tab === "runs" ? renderScheduleRuns(payload) : renderScheduleTemplates(payload);
    $("#content").innerHTML = scheduleChrome(body);
    $("#schedule-create").onclick = () => openScheduleEditor();
    document.querySelectorAll("[data-schedule-tab]").forEach((button) => { button.onclick = () => renderSchedules(button.dataset.scheduleTab); });
    document.querySelectorAll("[data-task-edit]").forEach((button) => { button.onclick = () => openScheduleEditor(scheduleView.jobs.find((item) => item.config_key === button.dataset.taskEdit)); });
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
      <div><p class="eyebrow">${escapeHtml(protocol.id === "holdout_strategy" ? "PRIMARY PROTOCOL" : "LEGACY REPRODUCTION")}</p><h3>${escapeHtml(protocol.title)}</h3><p class="muted">${escapeHtml(protocol.description)}</p></div>
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
    <div class="exp-protocol-head"><div><p class="eyebrow">HOW TO READ</p><h3>指标说明</h3><p class="muted">这张看板为什么这样设计</p></div></div>
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
      <div><p class="eyebrow">EXPERIMENT LAB</p><h2>实验看板</h2></div>
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
      const frequencyList = (rows, empty) => rows.length ? rows.map(([name, count]) => `<li><span>${escapeHtml(name)}</span><strong>${count} 次</strong></li>`).join("") : `<li class="muted">${escapeHtml(empty)}</li>`;
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
    content.innerHTML = `<section class="memory-workspace"><div class="memory-toolbar"><div><p class="eyebrow">OBSERVABILITY</p><h2>运行看板</h2><p class="muted">实际程序记录 · 缺失指标保持未知</p></div></div><div class="memory-tabs">${tabsHtml}</div><div class="memory-body">${body}</div></section>`;
    document.querySelectorAll("[data-dashboard-tab]").forEach((button) => { button.onclick = () => renderDashboard(button.dataset.dashboardTab); });
  } catch (error) { content.innerHTML = emptyState("运行数据暂不可用", error.message); }
}

function activateSection(id) {
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
  $("#nav").innerHTML = `${modules}<div class="nav-divider"></div><button class="nav-item" data-target="dashboard"><span class="nav-glyph">${glyphs.dashboard}</span><span>运行看板</span></button><div class="nav-divider"></div><button class="nav-item" data-target="memory"><span class="nav-glyph">${glyphs.memory}</span><span>记忆中心</span></button><button class="nav-item" data-target="schedules"><span class="nav-glyph">${glyphs.schedules}</span><span>任务中心</span></button><button class="nav-item" data-target="experiments"><span class="nav-glyph">${glyphs.experiments}</span><span>实验看板</span></button><button class="nav-item" data-target="guide"><span class="nav-glyph">${glyphs.guide}</span><span>使用指南</span></button>`;
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
