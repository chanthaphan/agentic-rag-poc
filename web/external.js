// External chat page (/studio for the "external" role): the debug chat only. One conversation at a time, streamed
// from /chat/stream, with the trace card of the last answer on the side. No Studio tabs, no other people's sessions.
// Helpers ($, esc, api, json, md, readSSE, phaseText) come from studio-common.js.
let session = null; let busy = false;

async function send() {
  const q = $("#ext-q").value.trim(); if (!q || busy) return; $("#ext-q").value = ""; busy = true;
  const th = $("#ext-thread");
  th.insertAdjacentHTML("beforeend", `<div class="pg-q">${esc(q)}</div><div class="pg-a"><div class="pg-text muted">routing…</div></div>`);
  const textEl = th.lastElementChild.querySelector(".pg-text"); let text = "";
  try {
    const res = await fetch(new URL("/chat/stream", location.origin), { credentials: "same-origin", ...json({ session_id: session, message: q, force_skill: $("#force-skill").value || null, source: "external" }) });
    if (!res.ok) throw new Error(`${res.status} ${await errorDetail(res)}`);
    await readSSE(res, (ev) => {
      if (ev.type === "session") session = ev.session_id;
      else if (ev.type === "route") textEl.textContent = `routed to ${ev.skill_id || "?"}, the agent is starting…`;
      else if (ev.type === "status" && !text) textEl.textContent = phaseText(ev);
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

// A new conversation only drops the session id (like the Studio playground): the previous one stays on record so
// the team can review it under Conversations.
function reset() { session = null; $("#ext-thread").innerHTML = ""; $("#turn-info").innerHTML = '<span class="muted">new conversation</span>'; $("#ext-tools").hidden = true; }

$("#ext-send").addEventListener("click", send);
$("#ext-q").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
$("#ext-new").addEventListener("click", reset);

// handoff answers: the trace card's "check now" link pulls the specialist's tokens from the Foundry trace on demand
document.addEventListener("click", async (e) => {
  const a = e.target.closest(".tc-reconcile"); if (!a) return; e.preventDefault();
  const sid = a.dataset.session || session || ""; if (!sid) return; a.textContent = "checking…";
  try { const r = await api(`/sessions/${sid}/reconcile`, { method: "POST" }); a.textContent = r.updated ? "updated, ask again to see the new trace" : (r.enabled ? "not in the trace yet, try again in a minute" : "tracing not connected"); }
  catch (err) { a.textContent = err.message; }
});

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
