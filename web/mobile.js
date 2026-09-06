const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(new URL(path, location.origin), opts);
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(t); }
  return r.json();
};
const ICON = {
  sparkles: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/></svg>',
  corner: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 10 20 15 15 20"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/></svg>',
};
const TH = (navigator.language || "").toLowerCase().startsWith("th");
const state = { config: null, sessionId: null, turns: [], suggestions: [], busy: false };

// ---------- rendering ----------
function greeting(name) {
  const h = new Date().getHours();
  if (TH) return `สวัสดี${h < 12 ? "ตอนเช้า" : h < 18 ? "ตอนบ่าย" : "ตอนเย็น"} คุณ${name}`;
  return `Good ${h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"} Khun ${name}`;
}
function renderMarkdown(text) {
  // full markdown (bold, lists, tables, links) via marked + DOMPurify; falls back to escaped text if the libs are missing
  if (!window.marked || !window.DOMPurify) return esc(text).replace(/\n/g, "<br>");
  const html = marked.parse(text, { gfm: true, breaks: true });
  return DOMPurify.sanitize(html, { ADD_ATTR: ["target", "rel"] }).replace(/<a /g, '<a target="_blank" rel="noopener" ');
}
const linkify = renderMarkdown;
function render() {
  const body = $("#body");
  if (!state.turns.length && !state.busy) {
    const c = state.config || { user_name: "Pim", starter_prompts: [] };
    const byLang = c.starter_prompts_by_lang || {};
    c.starter_prompts = (TH ? byLang.th : byLang.en) && (TH ? byLang.th : byLang.en).length ? (TH ? byLang.th : byLang.en) : c.starter_prompts;
    body.innerHTML = `<div class="empty"><div class="greet">${esc(greeting(c.user_name))}</div>
      <h2>${TH ? "ยินดีช่วยเรื่องผลิตภัณฑ์<br>ธนาคารกรุงเทพค่ะ" : "I'm here to help you<br>with Bangkok Bank products"}</h2>
      <div class="chips">${c.starter_prompts.map((p, i) => `<button class="chip" style="animation-delay:${(i + 1) * 0.08}s" data-q="${esc(p)}">${ICON.sparkles}<span>${esc(p)}</span></button>`).join("")}</div></div>`;
    body.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => send(b.dataset.q)));
    renderLog();
    return;
  }
  let html = '<div class="thread">';
  state.turns.forEach((t, i) => {
    if (t.role === "user") { html += `<div class="row me"><div class="bubble">${esc(t.text)}</div></div>`; return; }
    const isLast = i === state.turns.length - 1;
    const badge = t.skill_id && t.skill_id !== "offtopic" ? `<button class="badge" data-turn="${i}">${esc(t.skill_id)} · ${Math.round((t.confidence || 0) * 100)}%</button>` : "";
    const cites = (t.citations || []).slice(0, 3).map((c) => `<a class="cite" href="${esc(c.url)}" target="_blank" rel="noopener" title="${esc(c.url)}">${esc(c.title || c.url.replace(/^https?:\/\//, ""))}</a>`).join("");
    const fb = t.streaming || t.error || !state.sessionId ? "" : `<span class="fb" data-idx="${i}"><button class="${t.rating === "up" ? "on" : ""}" data-r="up" title="helpful">👍</button><button class="${t.rating === "down" ? "on" : ""}" data-r="down" title="not helpful">👎</button></span>`;
    html += `<div class="row"><span class="ai-av">${ICON.sparkles}</span><div class="bubble md ${t.error ? "err" : ""}">${linkify(t.text)}${badge || cites || fb ? `<div class="meta">${badge}${cites}${fb}</div>` : ""}</div></div>`;
    if (isLast && !state.busy && (t.suggestions || []).length) {
      const lth = (t.language || (TH ? "th" : "en")) === "th";
      html += `<div class="suggest"><div class="lbl">${lth ? "คำถามที่เกี่ยวข้อง" : "Suggested"}</div>${t.suggestions.map((s) => `<button data-q="${esc(s)}">${ICON.corner}${esc(s)}</button>`).join("")}</div>`;
    }
  });
  if (state.busy) html += `<div class="typing"><span class="ai-av">${ICON.sparkles}</span><div class="dotsbox"><i></i><i></i><i></i></div></div>`;
  html += "</div>";
  body.innerHTML = html;
  body.querySelectorAll(".suggest button").forEach((b) => b.addEventListener("click", () => send(b.dataset.q)));
  body.querySelectorAll(".badge").forEach((b) => b.addEventListener("click", () => showSources(state.turns[+b.dataset.turn])));
  body.querySelectorAll(".fb button").forEach((b) => b.addEventListener("click", async () => {
    const idx = +b.parentElement.dataset.idx; const t = state.turns[idx]; const rating = t.rating === b.dataset.r ? null : b.dataset.r; t.rating = rating;
    try { await api("/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: state.sessionId, idx, rating, comment: "" }) }); } catch {}
    render();
  }));
  body.scrollTop = body.scrollHeight;
  renderLog();
}

// ---------- behind-the-scenes panel ----------
const { fmtUsd, fmtK, bar, traceCard } = TR;
function renderLog() {
  const el = $("#dp-log"); if (!el) return;
  $("#dp-session").textContent = state.sessionId ? `session ${state.sessionId}` : "";
  const blocks = []; let totCost = 0, totIn = 0, totOut = 0, n = 0;
  for (let i = 0; i < state.turns.length; i++) {
    const t = state.turns[i]; if (t.role !== "assistant") continue;
    const q = state.turns[i - 1] && state.turns[i - 1].role === "user" ? state.turns[i - 1].text : "";
    const tr = t.trace || {}; n++; totCost += (tr.cost || {}).total_usd || 0; totIn += ((tr.usage || {}).total || {}).input_tokens || 0; totOut += ((tr.usage || {}).total || {}).output_tokens || 0;
    blocks.push(traceCard(t, q));
  }
  if (state.busy) blocks.push(`<div class="tc"><div class="tc-q">${esc(state.turns[state.turns.length - 1]?.text || "")}</div><div class="tc-row"><span class="k">status</span><span class="v">routing with bank-router, then the skill agent retrieves and answers…</span></div></div>`);
  const totals = n ? `<div class="tc-totals"><span><b>${n}</b> answer${n > 1 ? "s" : ""}</span><span><b>${fmtK(totIn)}</b> in</span><span><b>${fmtK(totOut)}</b> out</span><span class="cost"><b>${fmtUsd(totCost)}</b> session</span></div>` : "";
  el.innerHTML = totals + (blocks.length ? blocks.join("") : '<div class="dp-empty">Send a message to see routing, agent, retrieval, tokens, cost and sources.</div>');
  el.scrollTop = el.scrollHeight;
}
$("#dp-toggle")?.addEventListener("click", () => { $("#devpanel").classList.add("hidden"); $("#dp-show").hidden = false; });
$("#dp-show")?.addEventListener("click", () => { $("#devpanel").classList.remove("hidden"); $("#dp-show").hidden = true; });

// ---------- chat ----------
async function readSSE(response, onEvent) {
  const reader = response.body.getReader(); const dec = new TextDecoder(); let buf = "";
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split("\n")) if (line.startsWith("data: ")) { let ev; try { ev = JSON.parse(line.slice(6)); } catch (e) { console.warn("bad sse", e); continue; } onEvent(ev); }
    }
  }
}
async function send(text) {
  const q = (text ?? $("#input").value).trim();
  if (!q || state.busy) return;
  $("#input").value = ""; updateSend();
  state.turns.push({ role: "user", text: q }); state.busy = true; render();
  const draft = { role: "assistant", text: "", streaming: true, suggestions: [] };
  let bubble = null;
  try {
    const res = await fetch(new URL("/chat/stream", location.origin), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: state.sessionId, message: q }) });
    if (!res.ok || !res.body) { let t = await res.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(t || res.statusText); }
    await readSSE(res, (ev) => {
      if (ev.type === "session") { state.sessionId = ev.session_id; try { localStorage.setItem("bankrag_session", state.sessionId); } catch {} }
      else if (ev.type === "route") {
        Object.assign(draft, { skill_id: ev.skill_id, confidence: ev.confidence, language: ev.language, route_reason: ev.reason, agent_name: ev.agent_name, trace: { timings_ms: { route: ev.route_ms } } });
        if (!state.turns.includes(draft)) { state.turns.push(draft); state.busy = false; render(); }
        bubble = $("#body .row:last-child .bubble");
        renderLog();
      } else if (ev.type === "delta") {
        draft.text += ev.text;
        if (bubble) { bubble.innerHTML = esc(draft.text).replace(/\n/g, "<br>"); $("#body").scrollTop = 1e9; }
      } else if (ev.type === "tool") { draft.tool_calls = [...(draft.tool_calls || []), { type: "mcp_call", name: ev.name, arguments: ev.arguments, error: ev.error }]; renderLog(); }
      else if (ev.type === "done") {
        const a = ev.answer; const idx = state.turns.indexOf(draft);
        const turn = { role: "assistant", text: a.text, language: a.language, trace: a.trace, skill_id: a.skill_id, confidence: a.confidence, citations: a.citations, references: a.references, suggestions: a.suggestions, route_reason: a.route_reason, agent_name: a.agent_name, tool_calls: a.tool_calls };
        if (idx >= 0) state.turns[idx] = turn; else state.turns.push(turn);
      } else if (ev.type === "error") { throw new Error(ev.message); }
    });
  } catch (err) {
    const idx = state.turns.indexOf(draft); if (idx >= 0) state.turns.splice(idx, 1);
    state.turns.push({ role: "assistant", text: (TH ? "ขออภัย เกิดข้อผิดพลาด: " : "Sorry, something went wrong: ") + err.message, error: true, suggestions: [] });
  }
  state.busy = false; render();
}
function updateSend() { $("#btn-send").classList.toggle("on", !!$("#input").value.trim()); }
$("#input").addEventListener("input", updateSend);
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
$("#btn-send").addEventListener("click", () => send());
$("#btn-mic").addEventListener("click", () => $("#btn-mic").classList.toggle("on"));

// ---------- sheets ----------
function openSheet(html) { $("#sheet").innerHTML = '<div class="grab"></div>' + html; $("#sheet-bg").classList.add("on"); }
function closeSheet() { $("#sheet-bg").classList.remove("on"); }
$("#sheet-bg").addEventListener("click", (e) => { if (e.target === $("#sheet-bg")) closeSheet(); });
function showSources(t) {
  const refs = t.references || [];
  openSheet(`<h3>${TH ? "แหล่งข้อมูล" : "Sources"} · ${esc(t.skill_id)}</h3>` + (refs.length ? refs.map((r) => `<div class="item"><div class="t">${esc(r.title)}</div><div class="s">${esc(r.doc_type)} · ${esc(r.product_name)} · score ${r.score == null ? "-" : Number(r.score).toFixed(2)}</div><a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.source_url)}</a></div>`).join("") : `<div class="s">${TH ? "ไม่มีเอกสารอ้างอิง" : "No references"}</div>`));
}
async function showHistory() {
  const list = await api("/sessions");
  openSheet(`<h3>${TH ? "ประวัติการสนทนา" : "Conversations"}</h3><button class="primary" id="new-chat">${TH ? "เริ่มแชทใหม่" : "New chat"}</button>` +
    (list.length ? list.map((s) => `<div class="item" data-id="${s.id}"><div class="t">${esc(s.title || "(empty)")}</div><div class="s">${new Date(s.updated_at).toLocaleString()} · ${s.turns} ${TH ? "ข้อความ" : "messages"}${s.id === state.sessionId ? " · current" : ""}</div></div>`).join("") : `<div class="s">${TH ? "ยังไม่มีประวัติ" : "No conversations yet"}</div>`));
  $("#new-chat").addEventListener("click", () => { state.sessionId = null; state.turns = []; try { localStorage.removeItem("bankrag_session"); } catch {} closeSheet(); render(); });
  $("#sheet").querySelectorAll(".item").forEach((el) => el.addEventListener("click", () => loadSession(el.dataset.id).then(closeSheet)));
}
$("#btn-history").addEventListener("click", showHistory);

async function loadSession(id) {
  try {
    const rec = await api(`/sessions/${id}`);
    let fb = []; try { fb = await api(`/feedback?session_id=${id}`); } catch {}
    state.sessionId = rec.id; state.turns = rec.turns; for (const f of fb) if (state.turns[f.idx]) state.turns[f.idx].rating = f.rating; try { localStorage.setItem("bankrag_session", rec.id); } catch {}
    render();
  } catch { state.sessionId = null; state.turns = []; render(); }
}

// ---------- init ----------
(async function init() {
  try { state.config = await api("/app/config"); $("#avatar").textContent = state.config.user_initials || "PW"; $("#title").textContent = state.config.assistant_name || "Assistant"; } catch {}
  let saved = null; try { saved = localStorage.getItem("bankrag_session"); } catch {}
  if (saved) await loadSession(saved); else render();
})();
