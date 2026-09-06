const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(t); }
  return r.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---- tabs ----
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  b.classList.add("active"); $("#tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "skills") loadSkills(); if (b.dataset.tab === "knowledge") loadKnowledge();
}));

// ---- health + skill options ----
async function init() {
  try { const h = await api("/health"); $("#health").textContent = h.configured ? `index ${h.index} · KB ${h.kb_reasoning_effort} · auth ${h.kb_mcp_auth}` : "not configured: fill .env"; } catch (e) { $("#health").textContent = "api offline"; }
  const skills = await api("/skills?remote=false");
  for (const sel of [$("#force-skill"), $("#kn-skill")]) {
    for (const s of skills) { const o = document.createElement("option"); o.value = s.id; o.textContent = s.id; sel.appendChild(o); }
  }
  const cats = new Set(skills.map((s) => s.product_category).filter((c) => !["all", "*", ""].includes(c)));
  for (const c of cats) { const o = document.createElement("option"); o.value = c; o.textContent = c; $("#kn-category").appendChild(o); }
}

// ---- chat ----
let sessionId = null;
function addMsg(cls, html) { const d = document.createElement("div"); d.className = "msg " + cls; d.innerHTML = html; $("#messages").appendChild(d); $("#messages").scrollTop = 1e9; return d; }
$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#chat-input").value.trim(); if (!q) return;
  $("#chat-input").value = ""; addMsg("user", esc(q));
  const pending = addMsg("bot", "<span class='muted'>routing and retrieving…</span>");
  try {
    const res = await api("/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId, message: q, force_skill: $("#force-skill").value || null }) });
    sessionId = res.session_id; const a = res.answer;
    const cites = a.citations.map((c) => `<a href="${esc(c.url)}" target="_blank">${esc(c.title || c.url)}</a>`).join("");
    pending.innerHTML = `<span class="badge">${esc(a.skill_id)} · ${(a.confidence * 100).toFixed(0)}%</span><div>${esc(a.text)}</div>${cites ? `<div class="cites">${cites}</div>` : ""}`;
    const refs = a.references.map((r) => `<div class="ref"><div class="t">${esc(r.title)}</div><div class="s">${esc(r.doc_type)} · ${esc(r.product_name)} · score ${r.score ?? "-"}</div><a href="${esc(r.source_url)}" target="_blank">${esc(r.source_url)}</a><div>${esc(r.snippet.slice(0, 220))}…</div></div>`).join("");
    $("#turn-info").innerHTML = `<div><b>skill</b> ${esc(a.skill_id)} (${(a.confidence * 100).toFixed(0)}%) — ${esc(a.route_reason)}</div><div><b>agent</b> ${esc(a.agent_name)} · conversation ${esc(a.conversation_id)}</div><h3>Tool calls</h3><pre>${esc(JSON.stringify(a.tool_calls, null, 1)).slice(0, 4000)}</pre><h3>Sources (direct KB retrieve)</h3>${refs || "<span class='muted'>none</span>"}`;
  } catch (err) { pending.innerHTML = `<span style="color:#b42318">error: ${esc(err.message)}</span>`; }
});
$("#chat-reset").addEventListener("click", async () => { if (sessionId) await api(`/chat/${sessionId}/reset`, { method: "POST" }); sessionId = null; $("#messages").innerHTML = ""; $("#turn-info").textContent = "new conversation"; });

// ---- jobs ----
async function pollJob(id, logEl, statusEl, done) {
  const t = setInterval(async () => {
    const j = await api(`/jobs/${id}`); logEl.textContent = j.log.join("\n"); logEl.scrollTop = 1e9; statusEl.textContent = `${j.kind}: ${j.status}`;
    if (j.status !== "running") { clearInterval(t); done && done(j); }
  }, 1500);
}

// ---- skills ----
async function loadSkills() {
  $("#skills-status").textContent = "loading (checks Foundry)…";
  const rows = await api("/skills"); const tb = $("#skills-table tbody"); tb.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr"); tr.className = "clickable";
    tr.innerHTML = `<td>${esc(r.id)}</td><td>${esc(r.name)}</td><td>${esc(r.product_category)}</td><td>${esc(r.model)}</td><td>${esc(r.agent)}</td><td class="state-${esc(r.state).replace(/[^a-z-]/g, "")}">${esc(r.state)}</td><td>${esc(r.version)}</td><td><button class="secondary sync-one" data-id="${esc(r.id)}">sync</button></td>`;
    tr.addEventListener("click", async (ev) => { if (ev.target.classList.contains("sync-one")) return; const s = await api(`/skills/${r.id}`); $("#skill-view").textContent = s.skill_md + (s.warnings.length ? "\n\n# warnings\n" + s.warnings.join("\n") : ""); });
    tb.appendChild(tr);
  }
  tb.querySelectorAll(".sync-one").forEach((b) => b.addEventListener("click", async () => { const j = await api(`/skills/sync?only=${b.dataset.id}`, { method: "POST" }); pollJob(j.job_id, $("#skills-log"), $("#skills-status"), loadSkills); }));
  $("#skills-status").textContent = `${rows.length} skills`;
}
$("#skills-refresh").addEventListener("click", loadSkills);
$("#skills-sync").addEventListener("click", async () => { const j = await api("/skills/sync", { method: "POST" }); pollJob(j.job_id, $("#skills-log"), $("#skills-status"), loadSkills); });
$("#skill-zip").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
  try { const r = await api("/skills/upload", { method: "POST", body: fd }); $("#skills-status").textContent = `installed ${r.id}` + (r.errors.length ? " with errors: " + r.errors.join("; ") : ""); loadSkills(); } catch (err) { $("#skills-status").textContent = "upload failed: " + err.message; }
  e.target.value = "";
});

// ---- knowledge ----
async function loadKnowledge() {
  const d = await api("/knowledge/stats"); const tb = $("#kn-table tbody"); tb.innerHTML = "";
  for (const r of d.rows) tb.innerHTML += `<tr><td>${esc(r.category)}</td><td>${r.files}</td><td>${r.manifest_chunks}</td><td>${r.indexed_chunks}</td></tr>`;
  $("#kn-index").textContent = d.error ? `index: ${d.error}` : `index documents ${d.index.document_count ?? "?"} · storage ${((d.index.storage_size || 0) / 1e6).toFixed(1)} MB · vectors ${((d.index.vector_index_size || 0) / 1e6).toFixed(1)} MB`;
}
$("#kn-refresh").addEventListener("click", loadKnowledge);
$("#kn-files").addEventListener("change", async (e) => {
  const fd = new FormData(); for (const f of e.target.files) fd.append("files", f);
  try { const r = await api(`/knowledge/upload?category=${encodeURIComponent($("#kn-category").value)}`, { method: "POST", body: fd }); $("#kn-status").textContent = `saved ${r.saved.length} file(s); run ingest`; loadKnowledge(); } catch (err) { $("#kn-status").textContent = "upload failed: " + err.message; }
  e.target.value = "";
});
$("#kn-ingest").addEventListener("click", async () => { const j = await api(`/knowledge/ingest?category=${encodeURIComponent($("#kn-category").value)}`, { method: "POST" }); pollJob(j.job_id, $("#kn-log"), $("#kn-status"), loadKnowledge); });
$("#kn-retrieve").addEventListener("click", async () => {
  const q = $("#kn-q").value.trim(); if (!q) return; $("#kn-refs").innerHTML = "<span class='muted'>retrieving…</span>";
  try { const refs = await api(`/knowledge/retrieve?q=${encodeURIComponent(q)}&skill=${encodeURIComponent($("#kn-skill").value)}`);
    $("#kn-refs").innerHTML = refs.length ? refs.map((r) => `<div class="ref"><div class="t">${esc(r.title)}</div><div class="s">${esc(r.doc_type)} · ${esc(r.product_name)} · score ${r.score ?? "-"}</div><a href="${esc(r.source_url)}" target="_blank">${esc(r.source_url)}</a><div>${esc(r.snippet)}</div></div>`).join("") : "<span class='muted'>no references</span>";
  } catch (err) { $("#kn-refs").innerHTML = `<span style="color:#b42318">${esc(err.message)}</span>`; }
});

init();
