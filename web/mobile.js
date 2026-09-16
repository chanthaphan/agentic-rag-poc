const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(new URL(path, location.origin), opts);
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(t); }
  return r.json();
};
const ICON = {
  sparkles: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/></svg>',
  pin: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/></svg>',
  corner: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 10 20 15 15 20"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/></svg>',
};
const TH = (navigator.language || "").toLowerCase().startsWith("th");
const state = { config: null, sessionId: null, turns: [], suggestions: [], busy: false };
// what the assistant is doing while the bubble is still empty (phases come from /chat/stream "status" events)
const STATUS = {
  th: { thinking: "กำลังคิดค่ะ…", choosing: "กำลังดูว่าเรื่องนี้ควรให้ใครดูแลค่ะ…", retrieving: "กำลังค้นหาข้อมูล{skill}ให้ค่ะ…", specialist: "กำลังส่งเรื่องให้ผู้เชี่ยวชาญด้าน{skill}ค่ะ…", drafting: "พบข้อมูลแล้ว กำลังเรียบเรียงคำตอบค่ะ…", relaying: "ได้คำตอบจากผู้เชี่ยวชาญแล้ว กำลังเรียบเรียงให้ค่ะ…" },
  en: { thinking: "Thinking…", choosing: "Working out who should handle this…", retrieving: "Looking up {skill} information…", specialist: "Handing this to the {skill} specialist…", drafting: "Found it, writing the answer…", relaying: "The specialist replied, putting the answer together…" },
};
const SKILL_TH = { "credit-card": "บัตรเครดิต", "debit-card": "บัตรเดบิต", wealth: "การลงทุน", insurance: "ประกัน", general: "ผลิตภัณฑ์ธนาคาร", "financial-knowledge": "การเงิน", "bank-profile": "ธนาคารกรุงเทพ", "bank-services": "สาขาและอัตราแลกเปลี่ยน" };
function skillLabel(id, lang) {
  if (!id) return lang === "th" ? "" : "product";
  if (lang === "th") return SKILL_TH[id] || id.replace(/-/g, " ");
  const row = ((state.config || {}).skills || []).find((x) => x.id === id);
  return row ? row.name.replace(/\s+(Advisor|Assistant)$/i, "") : id.replace(/-/g, " ");
}
// a skill with no Thai label must not drop an English id into a Thai sentence: use the label-free wording instead
const STATUS_TH_PLAIN = { retrieving: "กำลังค้นหาข้อมูลให้ค่ะ…", specialist: "กำลังส่งเรื่องให้ผู้เชี่ยวชาญค่ะ…" };
function statusText(t) {
  const lang = t.language === "en" ? "en" : t.language === "th" ? "th" : TH ? "th" : "en";
  const status = t.status || "thinking";
  if (lang === "th" && t.status_skill && !SKILL_TH[t.status_skill] && STATUS_TH_PLAIN[status]) return STATUS_TH_PLAIN[status];
  const tpl = STATUS[lang][status] || STATUS[lang].thinking;
  return tpl.replace("{skill}", skillLabel(t.status_skill, lang));
}
function statusHtml(t) { return `<span class="wait"><i class="spin"></i>${esc(statusText(t))}</span>`; }

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
function placesHtml(places) {
  // A map is worth more than an address for a place the customer has to walk to - and for an ATM, which has no hours
  // and no phone, it is nearly the whole answer. With a Google key the map is embedded; without one the card still
  // works and simply opens the customer's own map app, which is what they were going to do anyway.
  if (!(places || []).length) return "";
  const key = (state.config || {}).maps_key || "";
  return `<div class="places">` + places.map((p) => {
    const dist = p.distance_km != null ? `<span class="km">${p.distance_km} กม.</span>` : "";
    const map = key
      ? `<iframe class="map" loading="lazy" referrerpolicy="no-referrer-when-downgrade" title="${esc(p.name)}"
           src="https://www.google.com/maps/embed/v1/place?key=${encodeURIComponent(key)}&q=${p.lat},${p.lon}&zoom=16"></iframe>`
      : "";
    return `<a class="place" href="${esc(p.maps_url)}" target="_blank" rel="noopener">
      ${map}<span class="pin">${ICON.pin}</span>
      <span class="where"><b>${esc(p.name)}</b>${dist}<i>${esc(p.address || "")}</i></span>
    </a>`;
  }).join("") + `</div>`;
}

// Dislike reasons: multi-select chips shown with the comment box. Saved into the feedback comment as
// "Label; Label — free text" (English labels whatever the UI language, so Studio can group them).
const DISLIKE_REASONS = [
  ["wrong", "Wrong information", "ข้อมูลไม่ถูกต้อง"],
  ["off", "Did not answer the question", "ไม่ตรงคำถาม"],
  ["incomplete", "Not enough detail", "ข้อมูลไม่ครบ"],
  ["unclear", "Hard to understand", "เข้าใจยาก"],
  ["tone", "Wrong language or tone", "ภาษาหรือน้ำเสียงไม่เหมาะ"],
  ["slow", "Too slow", "ตอบช้า"],
];
function composeComment(keys, text) {
  const labels = DISLIKE_REASONS.filter(([k]) => keys.includes(k)).map(([, en]) => en);
  return [labels.join("; "), (text || "").trim()].filter(Boolean).join(" — ");
}
function parseComment(comment) {
  const [head, ...rest] = (comment || "").split(" — ");
  const labels = head.split("; ");
  const keys = DISLIKE_REASONS.filter(([, en]) => labels.includes(en)).map(([k]) => k);
  const text = keys.length ? rest.join(" — ") : comment || "";
  return { keys, text };
}
function noteHtml(turn, i) {
  const lth = (turn.language || (TH ? "th" : "en")) === "th";
  if (turn.askComment) {
    if (!turn.draft) turn.draft = parseComment(turn.comment);
    const chips = DISLIKE_REASONS.map(([k, en, th]) => `<button class="chip-r ${turn.draft.keys.includes(k) ? "on" : ""}" data-k="${k}">${lth ? th : en}</button>`).join("");
    const ready = turn.draft.keys.length || turn.draft.text.trim();
    return `<div class="fb-note" data-idx="${i}"><div class="lbl">${lth ? "คำตอบนี้มีปัญหาอะไร เลือกได้มากกว่าหนึ่งข้อ" : "What was wrong with this answer? Pick any that apply."}</div><div class="reasons">${chips}</div><textarea rows="2" maxlength="900" placeholder="${lth ? "รายละเอียดเพิ่มเติม (ถ้ามี)…" : "Anything else? (optional)…"}">${esc(turn.draft.text)}</textarea><div class="acts"><button class="fb-note-send" ${ready ? "" : "disabled"}>${lth ? "ส่ง" : "Send"}</button><button class="fb-note-skip">${lth ? "ข้าม" : "Skip"}</button></div></div>`;
  }
  if (turn.rating === "down" && turn.comment) {
    const p = parseComment(turn.comment);
    const shown = [...DISLIKE_REASONS.filter(([k]) => p.keys.includes(k)).map(([, en, th]) => lth ? th : en), p.text].filter(Boolean).join(" · ");
    return `<div class="fb-note saved" data-idx="${i}"><span>💬 ${esc(shown)}</span><button class="fb-note-edit">${lth ? "แก้ไข" : "edit"}</button></div>`;
  }
  return "";
}
async function saveFeedback(idx, rating, comment) {
  try { await api("/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: state.sessionId, idx, rating, comment: comment || "" }) }); } catch {}
}

function render() {
  const body = $("#body");
  if (!state.turns.length && !state.busy) {
    const c = state.config || { user_name: "Pim", starter_prompts: [] };
    const byLang = c.starter_prompts_by_lang || {};
    c.starter_prompts = (TH ? byLang.th : byLang.en) && (TH ? byLang.th : byLang.en).length ? (TH ? byLang.th : byLang.en) : c.starter_prompts;
    body.innerHTML = `<div class="empty"><div class="greet">${esc(greeting(c.user_name))}</div>
      <h2>${TH ? `${esc(c.assistant_name || "เกรส")} ยินดีช่วยเรื่องผลิตภัณฑ์<br>ธนาคารกรุงเทพค่ะ` : `I'm ${esc(c.assistant_name || "Grace")}, here to help you<br>with Bangkok Bank products`}</h2>
      <div class="chips">${c.starter_prompts.map((p, i) => `<button class="chip" style="animation-delay:${(i + 1) * 0.08}s" data-q="${esc(p)}">${ICON.sparkles}<span>${esc(p)}</span></button>`).join("")}</div></div>`;
    body.querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => send(b.dataset.q)));
    renderLog();
    return;
  }
  let html = '<div class="thread">';
  state.turns.forEach((t, i) => {
    if (t.role === "user") { html += `<div class="row me"><div class="bubble">${esc(t.text)}</div></div>`; return; }
    const isLast = i === state.turns.length - 1;
    if (t.streaming) { html += `<div class="row"><span class="ai-av">${ICON.sparkles}</span><div class="bubble md">${t.text ? esc(t.text).replace(/\n/g, "<br>") : statusHtml(t)}</div></div>`; return; }
    const badge = t.skill_id && t.skill_id !== "offtopic" ? `<button class="badge" data-turn="${i}">${esc(t.skill_id)} · ${Math.round((t.confidence || 0) * 100)}%</button>` : "";
    const cites = (t.citations || []).slice(0, 3).map((c) => `<a class="cite" href="${esc(c.url)}" target="_blank" rel="noopener" title="${esc(c.url)}">${esc(c.title || c.url.replace(/^https?:\/\//, ""))}</a>`).join("");
    const fb = t.streaming || t.error || !state.sessionId ? "" : `<span class="fb" data-idx="${i}"><button class="${t.rating === "up" ? "on" : ""}" data-r="up" title="helpful">👍</button><button class="${t.rating === "down" ? "on" : ""}" data-r="down" title="not helpful">👎</button></span>`;
    html += `<div class="row"><span class="ai-av">${ICON.sparkles}</span><div class="bubble md ${t.error ? "err" : ""}">${linkify(t.text)}${placesHtml(t.places)}${badge || cites || fb ? `<div class="meta">${badge}${cites}${fb}</div>` : ""}${fb ? noteHtml(t, i) : ""}</div></div>`;
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
    // a dislike opens the comment box (the rating is saved at once, the comment follows); anything else clears the comment
    t.askComment = rating === "down"; t.draft = null; if (rating !== "down") t.comment = "";
    await saveFeedback(idx, rating, rating === "down" ? t.comment || "" : "");
    render();
  }));
  body.querySelectorAll(".fb-note .chip-r").forEach((b) => b.addEventListener("click", () => {
    const turn = state.turns[+b.closest(".fb-note").dataset.idx]; const k = b.dataset.k;
    turn.draft.keys = turn.draft.keys.includes(k) ? turn.draft.keys.filter((x) => x !== k) : [...turn.draft.keys, k];
    render();
  }));
  body.querySelectorAll(".fb-note textarea").forEach((ta) => {
    const turn = state.turns[+ta.closest(".fb-note").dataset.idx];
    ta.addEventListener("input", () => { turn.draft.text = ta.value; ta.closest(".fb-note").querySelector(".fb-note-send").disabled = !(turn.draft.keys.length || ta.value.trim()); });
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); const s = ta.closest(".fb-note").querySelector(".fb-note-send"); if (!s.disabled) s.click(); } });
  });
  body.querySelectorAll(".fb-note-send").forEach((b) => b.addEventListener("click", async () => {
    const idx = +b.closest(".fb-note").dataset.idx; const turn = state.turns[idx]; const comment = composeComment(turn.draft.keys, turn.draft.text); if (!comment) return;
    turn.comment = comment; turn.draft = null; turn.askComment = false;
    await saveFeedback(idx, turn.rating, comment); render();
  }));
  body.querySelectorAll(".fb-note-skip").forEach((b) => b.addEventListener("click", () => { const turn = state.turns[+b.closest(".fb-note").dataset.idx]; turn.askComment = false; turn.draft = null; render(); }));
  body.querySelectorAll(".fb-note-edit").forEach((b) => b.addEventListener("click", () => { const turn = state.turns[+b.closest(".fb-note").dataset.idx]; turn.askComment = true; turn.draft = null; render(); }));
  const openNote = body.querySelector(".fb-note textarea"); if (openNote && !body.querySelector(".fb-note .chip-r:focus")) { openNote.focus(); openNote.selectionStart = openNote.value.length; }
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
    const t = state.turns[i]; if (t.role !== "assistant" || t.streaming) continue;
    const q = state.turns[i - 1] && state.turns[i - 1].role === "user" ? state.turns[i - 1].text : "";
    const tr = t.trace || {}; n++; totCost += (tr.cost || {}).total_usd || 0; totIn += ((tr.usage || {}).total || {}).input_tokens || 0; totOut += ((tr.usage || {}).total || {}).output_tokens || 0;
    blocks.push(traceCard(t, q));
  }
  if (state.busy || state.turns.some((t) => t.streaming)) blocks.push(`<div class="tc"><div class="tc-q">${esc(([...state.turns].reverse().find((t) => t.role === "user") || {}).text || "")}</div><div class="tc-row"><span class="k">status</span><span class="v">${esc(statusText({ ...(state.turns.find((t) => t.streaming) || {}), language: "en" }))}</span></div></div>`);
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
// "where is the nearest branch" needs the customer's position. Ask the browser only for questions that are actually
// about a place, so nobody sees a location prompt for a question about an annual fee, and never block the answer on it.
const PLACE_RE = /(สาขา|ใกล้ฉัน|ใกล้ ?ๆ|แถวนี้|ที่ไหน|อยู่ไหน|แลกเงิน|ตู้ ?atm|เอทีเอ็ม|branch|near ?me|nearby|where.*(branch|exchange|atm)|atm)/i;
function currentPosition(timeoutMs = 6000) {
  if (!navigator.geolocation || !PLACE_RE.test(currentPosition.q || "")) return Promise.resolve(null);
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    setTimeout(() => finish(null), timeoutMs);  // a slow or ignored permission prompt must not hold the answer
    navigator.geolocation.getCurrentPosition(
      (p) => finish({ lat: p.coords.latitude, lon: p.coords.longitude }),
      () => finish(null),  // declined or unavailable: the assistant asks which province instead
      { enableHighAccuracy: false, timeout: timeoutMs, maximumAge: 300000 },
    );
  });
}

async function send(text) {
  const q = (text ?? $("#input").value).trim();
  if (!q || state.busy) return;
  $("#input").value = ""; updateSend();
  state.turns.push({ role: "user", text: q }); state.busy = true; render();
  const draft = { role: "assistant", text: "", streaming: true, suggestions: [] };
  let bubble = null;
  try {
    currentPosition.q = q;
    const here = await currentPosition();
    const res = await fetch(new URL("/chat/stream", location.origin), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: state.sessionId, message: q, ...(here || {}) }) });
    if (!res.ok || !res.body) { let t = await res.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(t || res.statusText); }
    await readSSE(res, (ev) => {
      if (ev.type === "session") { state.sessionId = ev.session_id; try { localStorage.setItem("bankrag_session", state.sessionId); } catch {} }
      else if (ev.type === "route") {
        Object.assign(draft, { skill_id: ev.skill_id, confidence: ev.confidence, language: ev.language, route_reason: ev.reason, agent_name: ev.agent_name, trace: { timings_ms: { route: ev.route_ms } } });
        if (!state.turns.includes(draft)) { state.turns.push(draft); state.busy = false; render(); }
        bubble = $("#body .row:last-child .bubble");
        renderLog();
      } else if (ev.type === "status") {
        draft.status = ev.phase; draft.status_skill = ev.skill_id || draft.status_skill || "";
        if (bubble && !draft.text) bubble.innerHTML = statusHtml(draft);
        renderLog();
      } else if (ev.type === "delta") {
        draft.text += ev.text;
        if (bubble) { bubble.innerHTML = esc(draft.text).replace(/\n/g, "<br>"); $("#body").scrollTop = 1e9; }
      } else if (ev.type === "tool") { draft.tool_calls = [...(draft.tool_calls || []), { type: "mcp_call", name: ev.name, arguments: ev.arguments, error: ev.error }]; renderLog(); }
      else if (ev.type === "done") {
        const a = ev.answer; const idx = state.turns.indexOf(draft);
        const turn = { role: "assistant", text: a.text, language: a.language, trace: a.trace, skill_id: a.skill_id, confidence: a.confidence, citations: a.citations, references: a.references, suggestions: a.suggestions, route_reason: a.route_reason, agent_name: a.agent_name, tool_calls: a.tool_calls, places: a.places };
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
// ---------- voice input (browser Web Speech API; Thai/English; needs HTTPS or localhost) ----------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, listening = false;
function speechLang() {
  const last = [...state.turns].reverse().find((t) => t.role === "user");
  const th = last ? /[\u0e00-\u0e7f]/.test(last.text) : TH;
  return th ? "th-TH" : "en-US";
}
function stopListening() { if (rec) { try { rec.stop(); } catch {} } }
function startListening() {
  if (!SR) { $("#input").placeholder = TH ? "เบราว์เซอร์นี้ไม่รองรับการพูด (ใช้ Chrome หรือ Safari)" : "Voice input not supported in this browser (use Chrome or Safari)"; return; }
  rec = new SR(); rec.lang = speechLang(); rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 1;
  let finalText = "";
  rec.onstart = () => { listening = true; $("#btn-mic").classList.add("on"); $("#input").placeholder = TH ? "กำลังฟัง… พูดได้เลย" : "Listening… speak now"; $("#input").value = ""; };
  rec.onresult = (e) => { let interim = ""; for (let i = e.resultIndex; i < e.results.length; i++) { const t = e.results[i][0].transcript; if (e.results[i].isFinal) finalText += t; else interim += t; } $("#input").value = (finalText + interim).trim(); updateSend(); };
  rec.onerror = (e) => { $("#input").placeholder = (TH ? "ไม่สามารถฟังได้: " : "Could not listen: ") + e.error; };
  rec.onend = () => { listening = false; $("#btn-mic").classList.remove("on"); const q = $("#input").value.trim(); $("#input").placeholder = "Ask anything"; if (q && finalText) send(q); };
  try { rec.start(); } catch (e) { $("#input").placeholder = "Could not start microphone: " + e.message; }
}
$("#btn-mic").addEventListener("click", () => (listening ? stopListening() : startListening()));
if (!SR) $("#btn-mic").title = "Voice input needs Chrome or Safari";

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
    state.sessionId = rec.id; state.turns = rec.turns; for (const f of fb) if (state.turns[f.idx]) { state.turns[f.idx].rating = f.rating; state.turns[f.idx].comment = f.comment || ""; } try { localStorage.setItem("bankrag_session", rec.id); } catch {}
    render();
  } catch { state.sessionId = null; state.turns = []; render(); }
}

// ---------- init ----------
// Entra (Container Apps built-in auth): show who is signed in and a Sign out button when /.auth/me answers.
(async function authNav() {
  try {
    const r = await fetch(new URL("/.auth/me", location.origin), { credentials: "same-origin" });
    if (!r.ok) return;
    const me = await r.json(); const claims = (me[0] || me.clientPrincipal || {}).user_claims || [];
    const name = (claims.find((c) => c.typ === "name") || claims.find((c) => c.typ === "preferred_username") || {}).val || (me[0] || {}).user_id || "";
    $("#tn-signout").hidden = false;
    if (name) $("#topnav").insertAdjacentHTML("afterbegin", `<span class="tn-user">${esc(name)}</span>`);
  } catch {}
})();

(async function init() {
  // account popover: who is signed in through Microsoft (Easy Auth) and a sign-out link; local dev has no SSO
  $("#avatar").addEventListener("click", async (e) => {
    e.stopPropagation(); const box = $("#account"); if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false; $("#acc-name").textContent = "…"; $("#acc-email").textContent = "";
    try { const w = await api("/whoami"); $("#acc-name").textContent = w.name || w.email || (TH ? "ยังไม่ได้ลงชื่อเข้าใช้" : "Not signed in"); $("#acc-email").textContent = w.email || (w.sso ? "" : TH ? "โหมดทดสอบในเครื่อง ไม่มี Microsoft sign-in" : "local mode, no Microsoft sign-in"); $("#acc-signout").hidden = !w.sso; $("#acc-signout").textContent = TH ? "ออกจากระบบ Microsoft" : "Sign out of Microsoft"; }
    catch (err) { $("#acc-name").textContent = err.message; }
  });
  document.addEventListener("click", (e) => { if (!$("#account").contains(e.target)) $("#account").hidden = true; });
  try { state.config = await api("/app/config"); $("#avatar").textContent = state.config.user_initials || "PW"; $("#title").textContent = state.config.assistant_name || "Assistant"; } catch {}
  let saved = null; try { saved = localStorage.getItem("bankrag_session"); } catch {}
  if (saved) await loadSession(saved); else render();
})();

// handoff answers: pull the specialist's tokens from the Foundry trace on demand
document.addEventListener("click", async (e) => {
  const a = e.target.closest(".tc-reconcile"); if (!a) return; e.preventDefault();
  const sid = a.dataset.session || (window.state && state.sessionId) || ""; if (!sid) return; a.textContent = "checking…";
  try { const r = await api(`/sessions/${sid}/reconcile`, { method: "POST" }); a.textContent = r.updated ? "updated, reopen the trace" : (r.enabled ? "not in the trace yet, try again in a minute" : "tracing not connected"); if (r.updated && typeof loadSession === "function") loadSession(sid); }
  catch (err) { a.textContent = err.message; }
});
