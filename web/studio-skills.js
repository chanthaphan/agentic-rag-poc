// Skills tab: list + lint badges, editor (form, markdown body, preview, versions diff, playground, try routing).
let current = null; let lintCache = {}; window.skillDirty = false;

function setDirty(v) { window.skillDirty = v; $("#ed-dirty").hidden = !v; }
window.addEventListener("beforeunload", (e) => { if (window.skillDirty) { e.preventDefault(); e.returnValue = ""; } });
document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && current && $("#view-skills").classList.contains("on")) { e.preventDefault(); saveSkill().catch((err) => showMsgs([err.message], [])); } });

async function loadSkills() {
  $("#sk-status").textContent = "checking Foundry…";
  const [rows, lint] = await Promise.all([api("/skills"), api("/skills/lint").catch(() => ({}))]);
  S.skillsCache = rows; lintCache = lint; const tb = $("#sk-table tbody"); tb.innerHTML = "";
  for (const r of rows) {
    const findings = lint[r.id] || []; const warns = findings.filter((f) => f.level === "warn").length; const infos = findings.length - warns;
    const tr = document.createElement("tr"); tr.className = "row" + (current === r.id ? " sel" : ""); tr.dataset.id = r.id;
    tr.innerHTML = `<td><b>${esc(r.id)}</b><br><span class="muted">${esc(r.name)}</span></td><td>${esc(r.product_category)}</td><td>${esc(r.model)}</td><td>${esc(r.knowledge_base)}</td><td class="st-${esc(r.state).replace(/[^a-z-]/g, "")}">${esc(r.state)}</td><td>${esc(r.version)}</td><td>${warns ? `<span class="pill warn">${warns}</span>` : ""}${infos ? `<span class="pill info">${infos}</span>` : ""}${!findings.length ? '<span class="pill ok">ok</span>' : ""}</td>`;
    tr.addEventListener("click", () => { if (window.skillDirty && current !== r.id && !confirm("Discard unsaved changes?")) return; openSkill(r.id); });
    tb.appendChild(tr);
  }
  $("#sk-status").textContent = `${rows.length} skills`;
  if (current) renderLint();
}
S.loaders.skills = loadSkills;

function renderLint() {
  const f = lintCache[current] || [];
  $("#ed-lint").innerHTML = f.length ? f.map((x) => `<div class="lint-${x.level}">${x.level === "warn" ? "⚠" : "ℹ"} <b>${esc(x.code)}</b> ${esc(x.message)}</div>`).join("") : '<div class="lint-ok">✓ lint: no findings</div>';
}
async function openSkill(id) {
  const d = await api(`/skills/${id}`); current = id; const fm = d.frontmatter;
  $("#sk-editor").hidden = false; $("#ed-title").textContent = `${id}  ·  skills/${id}/SKILL.md`;
  const row = S.skillsCache.find((r) => r.id === id); $("#ed-state").textContent = row ? `Foundry: ${row.state} (version ${row.version || "-"})` : "";
  $("#f-name").value = fm.name || ""; $("#f-description").value = fm.description || ""; $("#f-category").value = fm.product_category || id;
  $("#f-keywords").value = (fm.keywords || []).join(", "); fillModelSelect($("#f-model"), fm.model || "gpt-4.1-mini"); $("#f-topk").value = fm.top_k || 5;
  $("#f-suggestions").value = (fm.suggestions || []).join("\n"); $("#f-body").value = d.body || ""; updateLines();
  $("#ed-zip").href = `/skills/${id}/zip`; showMsgs(d.errors, d.warnings); renderLint(); setDirty(false);
  $$("#sk-table tr.row").forEach((tr) => tr.classList.toggle("sel", tr.dataset.id === id));
  $("#ver-table tbody").innerHTML = ""; $("#ver-diff").textContent = ""; $("#ver-restore").hidden = true; $("#ver-status").textContent = "";
  pgReset();
  $$(".subtabs button[data-pane]")[0].click();
}
function showMsgs(errors, warnings) { $("#ed-msgs").innerHTML = (errors || []).map((e) => `<div class="err">✖ ${esc(e)}</div>`).join("") + (warnings || []).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join(""); }
function formPayload() {
  return { name: $("#f-name").value, description: $("#f-description").value, product_category: $("#f-category").value, keywords: $("#f-keywords").value,
    model: $("#f-model").value, top_k: +$("#f-topk").value || 5, suggestions: $("#f-suggestions").value, body: $("#f-body").value };
}
function updateLines() { $("#f-lines").textContent = `${$("#f-body").value.split("\n").length} lines · ${$("#f-body").value.length} chars`; }
["#f-name", "#f-description", "#f-category", "#f-keywords", "#f-model", "#f-topk", "#f-suggestions", "#f-body"].forEach((sel) => $(sel).addEventListener("input", () => { setDirty(true); if (sel === "#f-body") updateLines(); }));
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
    await pollJob(j.job_id, $("#sk-log"), $("#sk-status")); await loadSkills();
    const after = (S.skillsCache.find((x) => x.id === current) || {}).version || "-";
    $("#ed-state").textContent = `synced: version ${before} → ${after}`; pgBanner();
  } catch (e) { showMsgs([e.message], []); }
});
$("#ed-delete").addEventListener("click", async () => {
  if (!confirm(`Delete skill '${current}' and its Foundry agent / knowledge base?`)) return;
  const r = await api(`/skills/${current}?prune=true`, { method: "DELETE" }); $("#sk-editor").hidden = true; current = null; setDirty(false);
  if (r.job_id) pollJob(r.job_id, $("#sk-log"), $("#sk-status"), loadSkills); else loadSkills();
});
$("#sk-refresh").addEventListener("click", loadSkills);
$("#sk-sync-all").addEventListener("click", async () => { const j = await api("/skills/sync", { method: "POST" }); pollJob(j.job_id, $("#sk-log"), $("#sk-status"), loadSkills); });
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

// ---- preview ----
S.loaders["pane-preview"] = () => { $("#md-preview").innerHTML = md(`# ${$("#f-name").value}\n\n> ${$("#f-description").value}\n\n${$("#f-body").value}`); };

// ---- try routing ----
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
    for (const v of vs) { const tr = document.createElement("tr"); tr.className = "row"; tr.innerHTML = `<td>${esc(v.version)}</td><td>${v.created_at ? new Date(v.created_at * 1000 || v.created_at).toLocaleString() : ""}</td><td>${esc(v.model)}</td><td class="muted">${esc((v.metadata || {}).spec_hash || "")}</td>`; tr.addEventListener("click", () => showDiff(v.version)); tb.appendChild(tr); }
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
function pgBanner() { const row = S.skillsCache.find((x) => x.id === current); $("#pg-banner").textContent = row ? `Testing the deployed agent ${row.agent} (version ${row.version || "-"}, ${row.state}${row.state === "outdated" ? " · Save & sync to test your edits" : ""})` : ""; }
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
      else if (ev.type === "route") textEl.textContent = "retrieving and answering…";
      else if (ev.type === "delta") { text += ev.text; textEl.classList.remove("muted"); textEl.innerHTML = esc(text).replace(/\n/g, "<br>"); }
      else if (ev.type === "done") { const a = ev.answer; textEl.innerHTML = md(a.text); traceEl.innerHTML = TR.traceCard({ ...a, trace: a.trace }, q); }
      else if (ev.type === "error") throw new Error(ev.message);
    });
  } catch (e) { textEl.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  pgBusy = false; th.scrollTop = th.scrollHeight;
}
S.loaders["pane-playground"] = pgBanner;
