const $ = (selector) => document.querySelector(selector);

let token = sessionStorage.getItem("qunbot-token") || "";
let state = null;
let original = {};

const glyphs = {
  connection: "⌁", conversation: "◌", personality: "✦", proactive: "◴",
  moderation: "◇", media: "▣", system: "⚙", memory: "◎", schedules: "◷", guide: "?",
};

let memoryView = { tab: "overview", scope: "" };
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
  const labels = { active: "有效", candidate: "候选", archived: "归档", expired: "过期", superseded: "已替代", succeeded: "成功", failed: "失败", running: "进行中", approved: "已审核", rejected: "已拒绝" };
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
    <article class="metric-card"><span>长期记忆</span><strong>${memory.total || 0}</strong><small>${statuses.active || 0} 条当前有效</small></article>
    <article class="metric-card"><span>Topic</span><strong>${memory.topics || 0}</strong><small>由长期事实关联形成</small></article>
    <article class="metric-card"><span>群友档案</span><strong>${data.people || 0}</strong><small>身份与关系状态</small></article>
    <article class="metric-card"><span>黑话候选</span><strong>${data.slang || 0}</strong><small>包含待审核内容</small></article>
  </div>
  <div class="overview-grid">
    <article class="data-panel"><div class="panel-title"><h3>记忆状态</h3></div>
      <div class="status-stack">${Object.keys(statuses).length ? Object.entries(statuses).map(([key, value]) => `<div><span>${statusBadge(key)}</span><b>${value}</b></div>`).join("") : `<p class="muted">还没有长期记忆。</p>`}</div>
    </article>
    <article class="data-panel"><div class="panel-title"><h3>最近一次提炼</h3></div>
      ${latest ? `<dl class="fact-grid"><div><dt>状态</dt><dd>${statusBadge(latest.status)}</dd></div><div><dt>消息范围</dt><dd>${latest.start_message_id}—${latest.end_message_id}</dd></div><div><dt>采用 / 拒绝</dt><dd>${latest.accepted_count} / ${latest.rejected_count}</dd></div><div><dt>完成时间</dt><dd>${formatTime(latest.finished_at)}</dd></div></dl>${latest.error ? `<p class="inline-error">${escapeHtml(latest.error)}</p>` : ""}` : emptyState("尚未执行提炼", "达到消息间隔后会在后台自动运行。")}
    </article>
  </div>`;
}

function renderMemories(rows) {
  if (!rows.length) return emptyState("还没有长期记忆", "新的提炼结果会在这里显示，并附带来源证据和修订记录。");
  return `<div class="list-head"><div><h3>长期记忆</h3><p>点击一条记录查看证据、Topic 和修订历史。</p></div><input id="memory-filter" type="search" placeholder="搜索记忆内容"></div>
  <div class="record-list" id="memory-records">${rows.map((row) => `<button class="record-card" data-memory-id="${row.id}" data-filter="${escapeHtml(row.content.toLowerCase())}"><div class="record-main"><div class="record-meta">${statusBadge(row.status)}<span>${escapeHtml(row.fact_type)}</span><span>${escapeHtml(row.user_id === "_group_" ? "群共享" : row.user_id)}</span></div><strong>${escapeHtml(row.content)}</strong></div><div class="record-side"><b>${Math.round(Number(row.confidence || 0) * 100)}%</b><small>${formatTime(row.updated_at)}</small></div></button>`).join("")}</div>`;
}

function renderTopics(rows) {
  if (!rows.length) return emptyState("还没有长期 Topic", "带 Topic 的记忆提炼成功后会自动建立关联。");
  return `<div class="card-grid">${rows.map((row) => `<button class="topic-card" data-topic-id="${row.id}"><div class="topic-orbit"></div><span>${row.memory_count} 条记忆</span><h3>${escapeHtml(row.canonical_name)}</h3><p>${escapeHtml(row.summary || "暂无摘要")}</p><small>最近出现 ${formatTime(row.last_seen_at)}</small></button>`).join("")}</div>`;
}

function renderPeople(rows) {
  if (!rows.length) return emptyState("还没有人物档案", "群友发言后会建立分群身份和关系记录。");
  return `<div class="people-grid">${rows.map((row) => `<button class="person-card" data-group-id="${escapeHtml(row.group_id)}" data-user-id="${escapeHtml(row.user_id)}"><span class="avatar-dot">${escapeHtml((row.nickname || row.user_id).slice(0, 1))}</span><div><h3>${escapeHtml(row.card || row.nickname || row.user_id)}</h3><p>${escapeHtml(row.stage_label || "普通关系")} · ${escapeHtml(row.group_id)}</p><small>${escapeHtml(row.relationship_note || "暂无关系变化")}</small></div><strong>${row.affection || 0}</strong></button>`).join("")}</div>`;
}

function renderSlang(rows) {
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
  return `<div class="run-list">${rows.map((row) => `<article class="run-card"><div class="run-marker ${escapeHtml(row.status)}"></div><div><div class="record-meta">${statusBadge(row.status)}<span>${escapeHtml(row.strategy_version)}</span></div><h3>消息 ${row.start_message_id}—${row.end_message_id}</h3><p>处理 ${row.message_count} 条，采用 ${row.accepted_count} 条，拒绝 ${row.rejected_count} 条。</p>${row.error ? `<small class="inline-error">${escapeHtml(row.error)}</small>` : `<small>${formatTime(row.finished_at || row.started_at)}</small>`}</div></article>`).join("")}</div>`;
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
  memoryView.tab = tab;
  if (graphController) { graphController.destroy(); graphController = null; }
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.target === "memory"));
  $(".search").classList.add("hidden");
  $("#save").classList.add("hidden");
  $("#content").innerHTML = `<section class="memory-workspace loading-state">正在读取记忆数据…</section>`;
  history.replaceState(null, "", "#memory");
  try {
    const overview = await memoryApi("overview");
    let payload = null;
    if (tab === "items") payload = await memoryApi("items?limit=200");
    if (tab === "topics") payload = await memoryApi("topics?limit=200");
    if (tab === "people") payload = await memoryApi("people?limit=200");
    if (tab === "slang") payload = await memoryApi("slang?limit=200");
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
      : `<div class="graph-shell"><div class="graph-note"><b>关系图谱</b><span>滚轮缩放 · 拖动画布 · 拖拽节点 · 点击查看详情</span></div><canvas id="memory-graph" aria-label="记忆关系图谱"></canvas><div class="graph-legend"><span class="legend-topic">Topic</span><span class="legend-memory">记忆</span><span class="legend-person">群友</span><span class="legend-bot">Bot</span></div></div>`;
    $("#content").innerHTML = memoryChrome(overview, body);
    $("#memory-scope").onchange = (event) => { memoryView.scope = event.target.value; renderMemory(); };
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
    if (tab === "graph") graphController = createMemoryGraph($("#memory-graph"), payload);
  } catch (error) {
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
      if (dragged.kind === "memory") openMemory(dragged.ref_id);
      if (dragged.kind === "topic") openTopic(dragged.ref_id);
      if (dragged.kind === "person") openPerson(dragged.group_id, dragged.ref_id);
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

function activateSection(id) {
  if (id === "memory") {
    renderMemory();
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
  $("#nav").innerHTML = `${modules}<div class="nav-divider"></div><button class="nav-item" data-target="memory"><span class="nav-glyph">${glyphs.memory}</span><span>记忆中心</span></button><button class="nav-item" data-target="schedules"><span class="nav-glyph">${glyphs.schedules}</span><span>任务中心</span></button><button class="nav-item" data-target="guide"><span class="nav-glyph">${glyphs.guide}</span><span>使用指南</span></button>`;
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
    sessionStorage.setItem("qunbot-token", token);
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
  enter().catch(() => { sessionStorage.removeItem("qunbot-token"); token = ""; });
}

$("#detail-close").onclick = () => $("#detail-dialog").close();
$("#detail-dialog").onclick = (event) => {
  if (event.target === $("#detail-dialog")) $("#detail-dialog").close();
};
