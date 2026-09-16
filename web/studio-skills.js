// Skills tab: skill cards + lint badges, Foundry sync panel (progress, per-skill result rows, log), editor
// (routing/answering form, markdown body, lint, preview, versions diff, playground, try routing).
let current = null; let lintCache = {}; window.skillDirty = false;
const SY = { timer: null };

function setDirty(v) { window.skillDirty = v; $("#ed-dirty").hidden = !v; }
window.addEventListener("beforeunload", (e) => { if (window.skillDirty) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && current && $("#view-skills").classList.contains("on")) { e.preventDefault(); saveSkill().catch((err) => showMsgs([err.message], [])); } });

const STATE_LABEL = { "in-sync": "in sync", outdated: "outdated", missing: "not deployed", error: "error" };
async function loadSkills() {
  $("#sk-status").textContent = "checking Foundry…";
  const [rows, lint] = await Promise.all([api("/skills"), api("/skills/lint").catch(() => ({}))]);
  S.skillsCache = rows; lintCache = lint; const box = $("#sk-cards"); box.innerHTML = "";
  for (const r of rows) {
    const findings = lint[r.id] || []; const warns = findings.filter((f) => f.level === "warn").length; const infos = findings.length - warns;
    const st = String(r.state || "").replace(/[^a-z-]/g, "");
    const el = document.createElement("div"); el.className = "sk-card" + (current === r.id ? " sel" : ""); el.dataset.id = r.id;
    el.innerHTML = `<span class="id">${esc(r.id)}</span><span class="st" title="Foundry agent ${esc(r.agent || "")}"><span class="dot ${st}"></span>${esc(STATE_LABEL[st] || r.state)}${r.version ? ` · v${esc(r.version)}` : ""}</span>
      <span class="nm">${esc(r.name)}</span>
      <span class="meta"><span class="pill">${esc(r.product_category)}</span><span class="pill">${esc(r.model)}</span><span class="pill" title="knowledge base">${esc(r.knowledge_base || "no knowledge base")}</span>${r.registry_version ? `<span class="pill reg" title="published to the Foundry skill registry as bankrag-${esc(r.id)}">registry v${esc(r.registry_version)}</span>` : ""}${r.a2a ? '<span class="pill a2a" title="exposed as an A2A endpoint; the concierge can hand off to it">A2A</span>' : ""}${warns ? `<span class="pill warn" title="lint warnings">⚠ ${warns}</span>` : ""}${infos ? `<span class="pill info" title="lint notes">ℹ ${infos}</span>` : ""}</span>`;
    el.addEventListener("click", () => { if (window.skillDirty && current !== r.id && !confirm("Discard unsaved changes?")) return; openSkill(r.id); });
    box.appendChild(el);
  }
  const outdated = rows.filter((r) => r.state === "outdated").length, missing = rows.filter((r) => r.state === "missing").length;
  $("#sk-status").textContent = `${rows.length} skills` + (outdated ? ` · ${outdated} outdated` : "") + (missing ? ` · ${missing} not deployed` : "");
  if (current) { renderLint(); const row = rows.find((r) => r.id === current); if (row) setStatePill(row); }
}
S.loaders.skills = loadSkills;

function setStatePill(row) { const st = String(row.state || "").replace(/[^a-z-]/g, ""); const p = $("#ed-state"); p.className = `pill st-${st}`; p.textContent = `Foundry: ${STATE_LABEL[st] || row.state}${row.version ? ` · version ${row.version}` : ""}`; }
function renderLint() {
  const f = lintCache[current] || []; const warns = f.filter((x) => x.level === "warn").length;
  $("#ed-lint").innerHTML = f.length ? f.map((x) => `<div class="lint-${x.level}">${x.level === "warn" ? "⚠" : "ℹ"} <b>${esc(x.code)}</b> ${esc(x.message)}</div>`).join("") : '<div class="lint-ok">✓ no findings</div>';
  $("#ed-lint-summary").innerHTML = f.length ? `lint: ${warns ? `<span class="lint-warn">${warns} warning${warns > 1 ? "s" : ""}</span>` : ""}${warns && f.length - warns ? ", " : ""}${f.length - warns ? `${f.length - warns} note${f.length - warns > 1 ? "s" : ""}` : ""}` : '<span class="lint-ok">lint: ✓ no findings</span>';
  $("#ed-lintwrap").open = warns > 0;
}
async function openSkill(id) {
  const d = await api(`/skills/${id}`); current = id; const fm = d.frontmatter;
  $("#sk-empty").hidden = true; $("#sk-editor").hidden = false; $("#ed-title").textContent = fm.name || id; $("#ed-path").textContent = `skills/${id}/SKILL.md`;
  const row = S.skillsCache.find((r) => r.id === id); if (row) setStatePill(row);
  $("#f-name").value = fm.name || ""; $("#f-description").value = fm.description || ""; $("#f-category").value = fm.product_category || id;
  $("#f-keywords").value = (fm.keywords || []).join(", "); fillModelSelect($("#f-model"), fm.model || "gpt-4.1-mini"); $("#f-topk").value = fm.top_k || 5;
  $("#f-suggestions").value = (fm.suggestions || []).join("\n"); $("#f-body").value = d.body || ""; updateLines();
  $("#ed-zip").href = `/skills/${id}/zip`; showMsgs(d.errors, d.warnings); renderLint(); setDirty(false);
  $$("#sk-cards .sk-card").forEach((el) => el.classList.toggle("sel", el.dataset.id === id));
  $("#ver-table tbody").innerHTML = ""; $("#ver-diff").textContent = ""; $("#ver-restore").hidden = true; $("#ver-status").textContent = ""; $("#try-out").textContent = "";
  pgReset();
  $$(".subtabs button[data-pane]")[0].click();
  // stacked layout: the editor sits under the skill list, so bring it into view
  if (window.matchMedia("(max-width: 1100px)").matches) $("#sk-editor").scrollIntoView({ behavior: "smooth", block: "start" });
}
function showMsgs(errors, warnings) { $("#ed-msgs").innerHTML = (errors || []).map((e) => `<div class="err">✖ ${esc(e)}</div>`).join("") + (warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join(""); }
function formPayload() {
  return { name: $("#f-name").value, description: $("#f-description").value, product_category: $("#f-category").value, keywords: $("#f-keywords").value,
    model: $("#f-model").value, top_k: +$("#f-topk").value || 5, suggestions: $("#f-suggestions").value, body: $("#f-body").value };
}
function updateLines() { $("#f-lines").textContent = `${$("#f-body").value.split("\n").length} lines · ${$("#f-body").value.length} chars`; }
["#f-name", "#f-description", "#f-category", "#f-keywords", "#f-model", "#f-topk", "#f-suggestions", "#f-body"].forEach((sel) => $(sel).addEventListener("input", () => { setDirty(true); if (sel === "#f-body") updateLines(); if (sel === "#f-name") $("#ed-title").textContent = $("#f-name").value || current; }));
$("#f-body").addEventListener("keydown", (e) => { if (e.key === "Tab") { e.preventDefault(); const t = e.target; const s = t.selectionStart; t.value = t.value.slice(0, s) + "  " + t.value.slice(t.selectionEnd); t.selectionStart = t.selectionEnd = s + 2; setDirty(true); } });

async function saveSkill() {
  const r = await api(`/skills/${current}`, json(formPayload(), "PUT")); showMsgs(r.errors, r.warnings); $("#sk-status").textContent = `saved ${current}`; setDirty(false);
  lintCache = await api("/skills/lint").catch(() => lintCache); renderLint();
  return r;
}
$("#ed-save").addEventListener("click", () => saveSkill().catch((e) => showMsgs([e.message], [])));
$("#ed-save-sync").addEventListener("click", async () => {
  try {
    const r = await saveSkill(); if (r.errors.length) return;
    const before = (S.skillsCache.find((x) => x.id === current) || {}).version || "-";
    const j = await api(`/skills/sync?only=${current}`, { method: "POST" });
    await watchSync(j.job_id); await loadSkills();
    const after = (S.skillsCache.find((x) => x.id === current) || {}).version || "-";
    $("#sk-status").textContent = before === after ? `${current}: no change to deploy (version ${after})` : `${current}: deployed version ${before} → ${after}`; pgBanner();
  } catch (e) { showMsgs([e.message], []); }
});
$("#ed-delete").addEventListener("click", async () => {
  if (!confirm(`Delete skill '${current}' and its Foundry agent / knowledge base?`)) return;
  const r = await api(`/skills/${current}?prune=true`, { method: "DELETE" }); $("#sk-editor").hidden = true; $("#sk-empty").hidden = false; current = null; setDirty(false);
  if (r.job_id) { await watchSync(r.job_id); } loadSkills();
});
$("#sk-refresh").addEventListener("click", loadSkills);
$("#sk-sync-all").addEventListener("click", async () => { if (SY.timer) return; const j = await api("/skills/sync", { method: "POST" }); await watchSync(j.job_id); loadSkills(); });
$("#sk-zip").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
  try { const r = await api("/skills/upload", { method: "POST", body: fd }); $("#sk-status").textContent = `installed ${r.id}`; await loadSkills(); openSkill(r.id); } catch (err) { $("#sk-status").textContent = err.message; }
  e.target.value = "";
});
$("#sk-new").addEventListener("click", () => $("#dlg-new").showModal());
$("#dlg-new form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  try { const r = await api("/skills", json({ id: $("#n-id").value.trim(), name: $("#n-name").value, description: $("#n-description").value, keywords: $("#n-keywords").value }));
    $("#dlg-new").close(); await loadSkills(); openSkill(r.spec.id); } catch (err) { alert(err.message); }
});

// ---- Foundry sync panel ----
function watchSync(id) {
  clearInterval(SY.timer);
  $("#sync-barwrap").hidden = false; $("#sync-rows").innerHTML = ""; $("#sync-logwrap").open = false;
  return new Promise((resolve) => {
    const tick = async () => {
      let j; try { j = await api(`/jobs/${id}`); } catch (e) { clearInterval(SY.timer); SY.timer = null; $("#sync-progress").textContent = e.message; resolve(null); return; }
      renderSync(j);
      if (j.status !== "running") { clearInterval(SY.timer); SY.timer = null; resolve(j); }
    };
    tick(); SY.timer = setInterval(tick, 800);
  });
}
function renderSync(j) {
  const pr = j.progress || {}; const running = j.status === "running", failed = j.status === "error";
  const st = $("#sync-state"); st.textContent = running ? "syncing" : failed ? "failed" : "done"; st.className = "pill " + (running ? "run" : failed ? "err" : "ok");
  const fill = $("#sync-bar"); const pct = j.status === "done" ? 100 : pr.total ? Math.round(100 * (pr.done || 0) / pr.total) : null;
  fill.className = "fill" + (failed ? " err" : j.status === "done" ? " ok" : pct == null ? " indet" : ""); fill.style.width = pct == null ? "" : pct + "%";
  $("#sync-progress").textContent = running ? (pr.message ? `syncing ${pr.message}…` : "starting…") : `${j.kind} ${j.status} in ${(j.elapsed_ms / 1000).toFixed(1)} s`;
  const rows = (j.result && j.result.rows) || [];
  $("#sync-rows").innerHTML = rows.map((r) => `<div class="r"><span><b>${esc(r.skill_id)}</b> <span class="muted">${esc(r.agent || "")}${r.knowledge_base ? " · " + esc(r.knowledge_base) : ""}</span></span><span class="a-${esc(r.action)}">${esc(r.action)}${r.version ? ` v${esc(r.version)}` : ""}</span>${r.note ? `<span class="n">${esc(r.note)}</span>` : ""}</div>`).join("");
  const log = $("#sk-log"); log.textContent = (j.log || []).join("\n"); log.scrollTop = 1e9;
  if (failed || rows.some((r) => r.action === "error")) $("#sync-logwrap").open = true;
}

// ---- preview ----
S.loaders["pane-preview"] = async () => {
  $("#md-preview").innerHTML = md(`# ${$("#f-name").value}\n\n> ${$("#f-description").value}\n\n${$("#f-body").value}`);
  // the Responsible Lending rules covering this skill's products are appended to its instructions on sync
  try { const r = await api(`/rules/prompt?skill=${encodeURIComponent(current)}`); if (r.block) $("#md-preview").innerHTML += `<hr>${md(r.block)}`; } catch {}
};

// ---- try routing ----
$("#try-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#try-run").click(); });
$("#try-run").addEventListener("click", async () => {
  const q = $("#try-q").value.trim(); if (!q) return; $("#try-out").textContent = "routing…";
  try { const d = await api("/route", json({ message: q })); const hit = d.skill_id === current;
    $("#try-out").innerHTML = `<span class="pill ${hit ? "ok" : "warn"}">${esc(d.skill_id)} · ${Math.round(d.confidence * 100)}%</span> ${esc(d.reason)}${hit ? "" : ` <span class="muted">(expected ${esc(current)})</span>`}`; } catch (e) { $("#try-out").textContent = e.message; }
});

// ---- versions + diff ----
async function loadVersions() {
  $("#ver-status").textContent = "loading…";
  try {
    const vs = await api(`/skills/${current}/versions`); const tb = $("#ver-table tbody"); tb.innerHTML = "";
    for (const v of vs) { const tr = document.createElement("tr"); tr.className = "row"; tr.innerHTML = `<td>${esc(v.version)}</td><td>${v.created_at ? new Date(v.created_at * 1000 || v.created_at).toLocaleString() : ""}</td><td>${esc(v.model)}</td><td class="muted">${esc((v.metadata || {}).spec_hash || "")}</td>`; tr.addEventListener("click", () => { $$("#ver-table tr.row").forEach((x) => x.classList.toggle("sel", x === tr)); showDiff(v.version); }); tb.appendChild(tr); }
    $("#ver-status").textContent = `${vs.length} versions`;
  } catch (e) { $("#ver-status").textContent = e.message; }
}
S.loaders["pane-versions"] = () => { if (!$("#ver-table tbody").children.length) loadVersions(); };
$("#ver-refresh").addEventListener("click", loadVersions);
let verSelected = null;
async function showDiff(version) {
  const d = await api(`/skills/${current}/versions/${version}`); verSelected = d;
  $("#ver-title").textContent = `deployed v${version} (${d.model}) vs local (${d.local_model})`;
  const parts = Diff.diffLines(d.instructions, d.local_instructions);
  $("#ver-diff").innerHTML = parts.map((p) => `<span class="${p.added ? "add" : p.removed ? "del" : "same"}">${esc(p.value)}</span>`).join("");
  $("#ver-restore").hidden = false;
}
$("#ver-restore").addEventListener("click", () => {
  if (!verSelected) return;
  const m = verSelected.instructions.split(/\n# Skill: [^\n]*\n\n/); const body = (m[1] || verSelected.instructions).replace(/\n\n# Knowledge base status[\s\S]*$/, "").trim();
  $("#f-body").value = body; updateLines(); setDirty(true); $$(".subtabs button[data-pane]")[0].click();
});

// ---- playground ----
let pgSession = null; let pgBusy = false;
function pgReset() { pgSession = null; $("#pg-thread").innerHTML = ""; pgBanner(); }
function pgBanner() { const row = S.skillsCache.find((x) => x.id === current); $("#pg-banner").textContent = row ? `Testing the deployed agent ${row.agent} (version ${row.version || "-"}, ${STATE_LABEL[row.state] || row.state}${row.state === "outdated" ? " · Save & sync to test your edits" : ""})` : ""; }
$("#pg-new").addEventListener("click", pgReset);
$("#pg-q").addEventListener("keydown", (e) => { if (e.key === "Enter") pgSend(); });
$("#pg-send").addEventListener("click", pgSend);
async function pgSend() {
  const q = $("#pg-q").value.trim(); if (!q || pgBusy) return; $("#pg-q").value = ""; pgBusy = true;
  const th = $("#pg-thread");
  th.insertAdjacentHTML("beforeend", `<div class="pg-q">${esc(q)}</div><div class="pg-a"><div class="pg-text muted">routing…</div><div class="pg-trace"></div></div>`);
  const aEl = th.lastElementChild; const textEl = aEl.querySelector(".pg-text"); const traceEl = aEl.querySelector(".pg-trace"); let text = "";
  try {
    const res = await fetch(new URL("/chat/stream", location.origin), { credentials: "same-origin", ...json({ session_id: pgSession, message: q, force_skill: current, source: "studio" }) });
    if (!res.ok) throw new Error(await res.text());
    await readSSE(res, (ev) => {
      if (ev.type === "session") pgSession = ev.session_id;
      else if (ev.type === "route") textEl.textContent = "routed, the agent is starting…";
      else if (ev.type === "status" && !text) textEl.textContent = phaseText(ev);
      else if (ev.type === "delta") { text += ev.text; textEl.classList.remove("muted"); textEl.innerHTML = esc(text).replace(/\n/g, "<br>"); }
      else if (ev.type === "done") { const a = ev.answer; textEl.innerHTML = md(a.text); traceEl.innerHTML = TR.traceCard({ ...a, trace: a.trace }, q); }
      else if (ev.type === "error") throw new Error(ev.message);
    });
  } catch (e) { textEl.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  pgBusy = false; th.scrollTop = th.scrollHeight;
}
S.loaders["pane-playground"] = pgBanner;

// ---- Foundry skill registry ----
async function loadRegistry() {
  $("#rg-status").textContent = "loading…";
  try {
    const rows = await api("/registry/skills"); const box = $("#rg-list");
    box.innerHTML = rows.length ? rows.map((r) => `<div class="rg"><span><span class="n">${esc(r.name)}</span> <span class="pill">v${esc(r.default_version)}</span> ${r.in_app ? '<span class="pill ok">in app</span>' : '<span class="pill warn">registry only</span>'}<span class="d">${esc(r.description.slice(0, 120))}</span></span><span>${r.in_app ? "" : `<button class="btn-secondary imp" data-name="${esc(r.name)}" data-id="${esc(r.local_id)}">Import</button>`}</span></div>`).join("") : '<div class="muted">no skills in the registry yet: run Sync all</div>';
    box.querySelectorAll(".imp").forEach((b) => b.addEventListener("click", async () => {
      const id = prompt("Local skill id for this import (lowercase, dashes):", b.dataset.id); if (!id) return;
      try { const r = await api("/registry/import", json({ name: b.dataset.name, id })); $("#rg-status").textContent = r.note; await loadSkills(); openSkill(r.id); loadRegistry(); } catch (e) { $("#rg-status").textContent = e.message; }
    }));
    $("#rg-status").textContent = `${rows.length} in registry`;
  } catch (e) { $("#rg-status").textContent = e.message; }
}
$("#rg-refresh").addEventListener("click", loadRegistry);
