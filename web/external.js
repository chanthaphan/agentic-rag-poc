// External chat page (/studio for the "external" role): the debug chat only. One conversation at a time, streamed
// from /chat/stream, with the trace card of the last answer on the side. No Studio tabs, no other people's sessions.
const $ = (s) => document.querySelector(s);
const esc = TR.esc;
const md = (text) => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: true })) : esc(text);
const api = async (path, opts = {}) => {
  const r = await fetch(new URL(path, location.origin), { credentials: "same-origin", ...opts });
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(`${r.status} ${t}`); }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};
const json = (body, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

async function readSSE(response, onEvent) {
  const reader = response.body.getReader(); const dec = new TextDecoder(); let buf = "";
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split("\n")) if (line.startsWith("data: ")) { let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; } onEvent(ev); }
    }
  }
}

const PHASES = { choosing: "the concierge is choosing a specialist…", retrieving: "retrieving knowledge…", specialist: "handed over to the specialist…", drafting: "knowledge retrieved, writing the answer…", relaying: "specialist replied, the concierge is relaying…" };
let session = null; let busy = false;

async function send() {
  const q = $("#ext-q").value.trim(); if (!q || busy) return; $("#ext-q").value = ""; busy = true;
  const th = $("#ext-thread");
  th.insertAdjacentHTML("beforeend", `<div class="pg-q">${esc(q)}</div><div class="pg-a"><div class="pg-text muted">routing…</div></div>`);
  const textEl = th.lastElementChild.querySelector(".pg-text"); let text = "";
  try {
    const res = await fetch(new URL("/chat/stream", location.origin), { credentials: "same-origin", ...json({ session_id: session, message: q, force_skill: $("#force-skill").value || null, source: "external" }) });
    if (!res.ok) { let t = await res.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(`${res.status} ${t}`); }
    await readSSE(res, (ev) => {
      if (ev.type === "session") session = ev.session_id;
      else if (ev.type === "route") textEl.textContent = `routed to ${ev.skill_id || "?"}, the agent is starting…`;
      else if (ev.type === "status" && !text) textEl.textContent = (ev.phase === "retrieving" && ev.skill_id) ? `retrieving ${ev.skill_id} knowledge…` : (PHASES[ev.phase] || ev.phase);
      else if (ev.type === "delta") { text += ev.text; textEl.classList.remove("muted"); textEl.innerHTML = esc(text).replace(/\n/g, "<br>"); }
      else if (ev.type === "done") {
        const a = ev.answer; textEl.classList.remove("muted"); textEl.innerHTML = md(a.text);
        $("#turn-info").innerHTML = TR.traceCard({ ...a, session_id: ev.session_id }, q);
        const tools = a.tool_calls || []; $("#ext-tools").hidden = !tools.length; $("#ext-tools-json").textContent = JSON.stringify(tools, null, 1).slice(0, 6000);
      } else if (ev.type === "error") throw new Error(ev.message);
    });
  } catch (e) { textEl.classList.remove("muted"); textEl.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  busy = false; th.scrollTop = th.scrollHeight;
}

async function reset() {
  if (session) { try { await api(`/chat/${session}/reset`, { method: "POST" }); } catch {} }
  session = null; $("#ext-thread").innerHTML = ""; $("#turn-info").innerHTML = '<span class="muted">new conversation</span>'; $("#ext-tools").hidden = true;
}

$("#ext-send").addEventListener("click", send);
$("#ext-q").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
$("#ext-new").addEventListener("click", reset);

(async function authNav() { try { const r = await fetch(new URL("/.auth/me", location.origin), { credentials: "same-origin" }); if (r.ok) $("#ms-signout").hidden = false; } catch {} })();

(async function init() {
  try { const me = await api("/studio/me"); $("#who").textContent = me.tester || (me.sso && (me.sso.name || me.sso.email)) || ""; const rp = $("#role-pill"); rp.hidden = false; rp.textContent = me.role || "external"; } catch {}
  try { const h = await api("/health"); $("#health").textContent = h.configured ? `index ${h.index}` : "not configured"; } catch { $("#health").textContent = "api offline"; }
  try {
    const skills = await api("/skills?remote=false"); const sel = $("#force-skill");
    for (const s of skills) { const o = document.createElement("option"); o.value = s.id; o.textContent = s.name ? `${s.id} · ${s.name}` : s.id; sel.appendChild(o); }
  } catch {}
  $("#ext-q").focus();
})();
