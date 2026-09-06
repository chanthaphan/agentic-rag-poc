// Evals tab: editable question sets, routing / grounded runs, model comparison, structured run progress,
// results with KPI cards + failed-only filter, xlsx export per run and for the whole history, trend + run history.
const evState = { routing: [], rag: [], quality: [] };
const QM = { metrics: [], def: [] };
const EV = { timer: null, job: null, results: {}, runs: [], selected: null };
const SET_LABEL = { routing: "Routing", rag: "Grounded answers", quality: "Quality (DeepEval)", compare: "Model comparison" };
const fmtMsE = (ms) => ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;

// ---- question sets ----
function rowInput(v, cls, ph = "") { return `<input class="${cls}" value="${esc(v ?? "")}" placeholder="${esc(ph)}">`; }
function renderQuestions(set) {
  const tb = $(`#ev-${set} tbody`); tb.innerHTML = "";
  for (const [i, c] of evState[set].entries()) {
    const tr = document.createElement("tr");
    tr.innerHTML = set === "routing"
      ? `<td>${rowInput(c.q, "q", "customer question (Thai or English)")}</td><td>${rowInput(c.skill, "skill", "credit-card | offtopic")}</td><td><button class="btn-danger rm">✕</button></td>`
      : set === "quality"
      ? `<td>${rowInput(c.q, "q", "customer question")}</td><td>${rowInput(c.skill, "skill", "auto")}</td><td>${rowInput(c.expected_output, "expected", "what a correct answer must say (optional)")}</td><td><button class="btn-danger rm">✕</button></td>`
      : `<td>${rowInput(c.q, "q", "customer question")}</td><td>${rowInput(c.skill, "skill", "auto")}</td><td>${rowInput((c.expect || []).join(", "), "expect", "e.g. 150,000, Priority Pass")}</td><td><input type="checkbox" class="src" ${c.require_source !== false ? "checked" : ""}></td><td><button class="btn-danger rm">✕</button></td>`;
    tr.querySelector(".rm").addEventListener("click", () => { evState[set] = collect(set); evState[set].splice(i, 1); renderQuestions(set); });
    tb.appendChild(tr);
  }
  if (!evState[set].length) tb.innerHTML = `<tr><td colspan="5" class="muted">no questions yet — add one</td></tr>`;
}
function collect(set) {
  return Array.from($(`#ev-${set} tbody`).querySelectorAll("tr")).filter((tr) => tr.querySelector(".q")).map((tr) => set === "routing"
    ? { q: tr.querySelector(".q").value.trim(), skill: tr.querySelector(".skill").value.trim() }
    : set === "quality"
    ? { q: tr.querySelector(".q").value.trim(), skill: tr.querySelector(".skill").value.trim() || null, expected_output: tr.querySelector(".expected").value.trim() || null }
    : { q: tr.querySelector(".q").value.trim(), skill: tr.querySelector(".skill").value.trim() || null, expect: tr.querySelector(".expect").value.split(",").map((x) => x.trim()).filter(Boolean), require_source: tr.querySelector(".src").checked }).filter((c) => c.q);
}
async function loadEvals() {
  for (const set of ["routing", "rag", "quality"]) { try { evState[set] = await api(`/evals/${set}`); } catch { evState[set] = []; } renderQuestions(set); }
  await loadQualityMetrics();
  const sk = await api("/skills?remote=false"); $("#cmp-skill").innerHTML = sk.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("");
  await loadModels(); $("#cmp-models").innerHTML = S.models.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
  await loadEvalRuns();
  for (const set of ["routing", "rag", "quality", "compare"]) if (!EV.results[set]) { const latest = EV.runs.find((r) => r.set === set); if (latest) showRun(latest.id, false); }
}
S.loaders.evals = loadEvals;
for (const set of ["routing", "rag", "quality"]) {
  $(`#ev-add-${set}`).addEventListener("click", () => { evState[set] = collect(set); evState[set].push(set === "routing" ? { q: "", skill: "" } : set === "quality" ? { q: "", skill: null, expected_output: null } : { q: "", skill: null, expect: [], require_source: true }); renderQuestions(set); $(`#ev-${set} tbody tr:last-child .q`)?.focus(); });
  $(`#ev-save-${set}`).addEventListener("click", async () => { evState[set] = collect(set); try { await api(`/evals/${set}`, json(evState[set], "PUT")); $(`#ev-status-${set}`).textContent = `saved ${evState[set].length} questions`; } catch (e) { $(`#ev-status-${set}`).textContent = e.message; } });
  $(`#ev-run-${set}`).addEventListener("click", async () => {
    if (EV.timer) { $(`#ev-status-${set}`).textContent = "an eval is already running"; return; }
    evState[set] = collect(set); if (!evState[set].length) { $(`#ev-status-${set}`).textContent = "add at least one question"; return; }
    try {
      await api(`/evals/${set}`, json(evState[set], "PUT"));
      const j = set === "quality"
        ? await api("/evals/quality", json({ metrics: Array.from($$("#epane-quality .qm-list input:checked")).map((x) => x.value), threshold: +$("#qm-threshold").value || 0.7, judge_model: $("#qm-judge").value, limit: +$("#qm-limit").value || 0 }))
        : await api(`/evals/run?set=${set}`, { method: "POST" });
      $(`#ev-status-${set}`).textContent = ""; watchEval(j.job_id, set);
    } catch (e) { $(`#ev-status-${set}`).textContent = e.message; }
  });
  $(`.ev-failed-only[data-set=${set}]`).addEventListener("change", () => { if (EV.results[set]) renderRun(EV.results[set]); });
}
$("#cmp-run").addEventListener("click", async () => {
  const models = Array.from($("#cmp-models").selectedOptions).map((o) => o.value); const questions = $("#cmp-qs").value.split("\n").map((x) => x.trim()).filter(Boolean);
  if (models.length < 2 || !questions.length) { $("#cmp-status").textContent = "pick at least 2 models and 1 question"; return; }
  if (EV.timer) { $("#cmp-status").textContent = "an eval is already running"; return; }
  try { const j = await api("/evals/compare", json({ skill: $("#cmp-skill").value, models, questions })); $("#cmp-status").textContent = ""; watchEval(j.job_id, "compare"); } catch (e) { $("#cmp-status").textContent = e.message; }
});

// ---- upload a question list (.xlsx / .csv) ----
$$(".ev-upload").forEach((inp) => inp.addEventListener("change", async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return; const set = inp.dataset.set; const mode = $(`.ev-upload-mode[data-set=${set}]`).value;
  if (mode === "replace" && !confirm(`Replace every ${SET_LABEL[set]} question with the ${f.name} contents?`)) return;
  const fd = new FormData(); fd.append("file", f); $(`#ev-status-${set}`).textContent = "reading…";
  try { const r = await api(`/evals/${set}/upload?mode=${mode}`, { method: "POST", body: fd }); evState[set] = await api(`/evals/${set}`); renderQuestions(set);
    $(`#ev-status-${set}`).textContent = mode === "replace" ? `replaced: ${r.total} questions now` : `added ${r.added}, skipped ${r.skipped} duplicate(s), ${r.total} questions now`; } catch (err) { $(`#ev-status-${set}`).textContent = err.message; }
}));

// ---- quality: metric picker ----
async function loadQualityMetrics() {
  if (QM.metrics.length) return;
  try { const d = await api("/evals/quality/metrics"); QM.metrics = d.metrics; QM.def = d.default;
    for (const g of ["rag", "agentic"]) $(`#qm-${g}`).innerHTML = d.metrics.filter((m) => m.group === g).map((m) => `<label><input type="checkbox" value="${m.key}" ${d.default.includes(m.key) ? "checked" : ""}><span><b>${esc(m.label)}</b>${m.needs_expected ? ' <span class="pill info">needs expected answer</span>' : ""}<span class="d">${esc(m.description)}</span></span></label>`).join("");
    fillModelSelect($("#qm-judge"), d.judge_model);
  } catch (e) { $("#ev-status-quality").textContent = e.message; }
}
const metricLabel = (k) => (QM.metrics.find((m) => m.key === k) || {}).label || k;

// ---- run progress ----
function watchEval(id, set) {
  clearInterval(EV.timer); $("#ev-job").hidden = false; $("#ev-idle").hidden = true; $("#ev-logwrap").open = false;
  const tick = async () => {
    let j; try { j = await api(`/jobs/${id}`); } catch (e) { clearInterval(EV.timer); EV.timer = null; $("#ev-msg").textContent = e.message; return; }
    EV.job = j; renderEvalJob(j, set);
    if (j.status !== "running") { clearInterval(EV.timer); EV.timer = null; if (j.result) { EV.results[set] = j.result; renderRun(j.result); } await loadEvalRuns(); }
  };
  tick(); EV.timer = setInterval(tick, 800);
}
function renderEvalJob(j, set) {
  const pr = j.progress || {}; const st = pr.stats || {}; const running = j.status === "running", failed = j.status === "error";
  const pill = $("#ev-state"); pill.textContent = running ? "running" : failed ? "failed" : "done"; pill.className = "pill " + (running ? "run" : failed ? "err" : "ok");
  $("#ev-job-what").textContent = `${SET_LABEL[set] || set}${j.meta?.skill ? ` · ${j.meta.skill}` : ""}${j.meta?.models ? ` · ${j.meta.models.join(" vs ")}` : ""}`;
  const has = pr.total > 0 && pr.done != null; const pct = j.status === "done" ? 100 : has ? Math.round(100 * pr.done / pr.total) : null;
  const fill = $("#ev-bar"); fill.className = "fill" + (failed ? " err" : j.status === "done" ? " ok" : pct == null && running ? " indet" : ""); fill.style.width = pct == null ? "" : pct + "%";
  $("#ev-pct").textContent = failed ? "failed" : j.status === "done" ? "100%" : pct == null ? "…" : pct + "%";
  const unit = { questions: "questions", agents: "agents", cleanup: "" }[pr.phase] || "";
  $("#ev-count").textContent = running && has ? `${pr.done} / ${pr.total} ${unit}` : j.status === "done" ? "finished" : "";
  $("#ev-elapsed").textContent = fmtMsE(j.elapsed_ms || 0);
  $("#ev-msg").textContent = failed ? pr.message : running ? (pr.message || "") : ""; $("#ev-msg").classList.toggle("err", failed);
  $("#ev-chips").innerHTML = ["passed", "failed"].filter((k) => st[k] != null).map((k) => `<span class="igchip ${k === "passed" ? "added" : "errors"}"><b>${st[k]}</b>${k}</span>`).join("");
  const log = $("#ev-log"); log.textContent = (j.log || []).join("\n"); log.scrollTop = 1e9;
  if (failed) $("#ev-logwrap").open = true;
}

// ---- results ----
function renderRun(run) {
  const set = run.set; const sm = run.summary || {}; const rows = run.rows || [];
  const panel = $(`#ev-result-${set}-panel`); panel.hidden = false;
  $(`#ev-result-${set}-title`).textContent = `${SET_LABEL[set]} result`;
  $(`#ev-result-${set}-when`).textContent = `${run.started_at ? new Date(run.started_at).toLocaleString() : ""} · run ${run.id}`;
  $(`#ev-xlsx-${set}`).href = `/evals/runs/${run.id}.xlsx`;
  const el = $(`#ev-result-${set}`);
  if (set === "compare") {
    const models = sm.models || [];
    const sumTable = `<table class="list cmp-sum"><thead><tr><th>model</th><th>avg latency</th><th>total cost</th><th>errors</th></tr></thead><tbody>${models.map((m) => { const errs = rows.filter((r) => (r.by_model || {})[m]?.error).length; return `<tr><td><b>${esc(m)}</b></td><td>${fmtMsE(sm[`${m} avg_ms`] || 0)}</td><td>${fmtUsd(sm[`${m} cost_usd`])}</td><td>${errs || ""}</td></tr>`; }).join("")}</tbody></table>`;
    el.innerHTML = `<div class="cards"><div class="card"><div class="v">${esc(sm.skill || "")}</div><div class="k">skill</div></div><div class="card"><div class="v">${rows.length}</div><div class="k">questions</div></div><div class="card"><div class="v">${models.length}</div><div class="k">models</div></div></div>${sumTable}
      <table class="list"><thead><tr><th style="width:22%">question</th>${models.map((m) => `<th>${esc(m)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r.q)}</td>${models.map((m) => { const x = (r.by_model || {})[m] || {}; return `<td><div class="cmp-a">${x.error ? `<span class="err">${esc(x.error)}</span>` : md(x.text || "")}</div><div class="muted">${x.ms ? fmtMsE(x.ms) : ""} · ${fmtUsd(x.cost_usd)} · ${x.input_tokens || 0} in / ${x.output_tokens || 0} out${x.retrieval_calls ? ` · ${x.retrieval_calls} retrieval` : ""}</div></td>`; }).join("")}</tr>`).join("")}</tbody></table>`;
    return;
  }
  if (set === "quality") { renderQuality(run, el); return; }
  const score = sm.accuracy ?? sm.pass_rate ?? 0; const failedOnly = $(`.ev-failed-only[data-set=${set}]`).checked;
  const shown = failedOnly ? rows.filter((r) => !r.pass) : rows;
  el.innerHTML = `<div class="cards"><div class="card"><div class="v">${Math.round(score * 100)}%</div><div class="k">${set === "routing" ? "accuracy" : "pass rate"}</div></div><div class="card"><div class="v">${sm.passed ?? 0} / ${sm.questions ?? rows.length}</div><div class="k">passed</div></div><div class="card"><div class="v">${fmtMsE(sm.avg_ms || 0)}</div><div class="k">avg latency</div></div><div class="card"><div class="v">${fmtUsd(sm.total_cost_usd)}</div><div class="k">total cost</div></div></div>
    <div class="ev-kpi-bar" title="${sm.passed ?? 0} passed, ${(sm.questions ?? rows.length) - (sm.passed ?? 0)} failed"><div class="p" style="width:${Math.round(score * 100)}%"></div></div>
    <table class="list"><thead><tr><th style="width:60px"></th><th>question</th><th>expected</th><th>got</th><th style="width:60px">conf</th><th style="width:70px">latency</th><th style="width:70px">cost</th></tr></thead><tbody>${shown.map((r) => `<tr class="${r.pass ? "pass" : "fail"}"><td>${r.pass ? '<span class="pill ok">pass</span>' : '<span class="pill warn">fail</span>'}</td><td>${esc(r.q)}</td><td>${esc(r.expected)}</td><td>${esc(r.got)}${r.detail ? `<br><span class="muted">${esc(r.detail)}</span>` : ""}</td><td>${r.confidence != null ? Math.round(r.confidence * 100) + "%" : ""}</td><td>${r.ms != null ? fmtMsE(r.ms) : ""}</td><td>${fmtUsd(r.cost_usd)}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">${failedOnly ? "nothing failed" : "no rows"}</td></tr>`}</tbody></table>`;
}
function renderQuality(run, el) {
  const sm = run.summary || {}; const rows = run.rows || []; const keys = sm.metrics || []; const failedOnly = $(".ev-failed-only[data-set=quality]").checked;
  const shown = failedOnly ? rows.filter((r) => !r.pass) : rows; const avg = sm.avg_scores || {};
  const chip = (k, s) => !s || s.success == null ? `<span class="qs skip" title="${esc(s?.reason || "not scored")}">${esc(metricLabel(k))} –</span>` : `<span class="qs ${s.success ? "ok" : "bad"}" title="${esc(s.reason || "")}">${esc(metricLabel(k))} <b>${(s.score ?? 0).toFixed(2)}</b></span>`;
  el.innerHTML = `<div class="cards"><div class="card"><div class="v">${Math.round((sm.pass_rate || 0) * 100)}%</div><div class="k">pass rate (all metrics ≥ ${sm.threshold ?? 0.7})</div></div><div class="card"><div class="v">${sm.passed ?? 0} / ${sm.questions ?? rows.length}</div><div class="k">passed</div></div><div class="card"><div class="v">${esc(sm.judge_model || "")}</div><div class="k">judge</div></div><div class="card"><div class="v">${fmtMsE(sm.avg_ms || 0)}</div><div class="k">avg answer latency</div></div><div class="card"><div class="v">${fmtUsd(sm.total_cost_usd)}</div><div class="k">answer cost (judge not included)</div></div></div>
    <div class="avg">${keys.map((k) => `<span class="qs ${avg[k] == null ? "skip" : avg[k] >= (sm.threshold ?? 0.7) ? "ok" : "bad"}" title="average ${esc(metricLabel(k))}">avg ${esc(metricLabel(k))} <b>${avg[k] == null ? "–" : avg[k].toFixed(2)}</b></span>`).join("")}</div>
    <table class="list"><thead><tr><th style="width:60px"></th><th style="width:26%">question</th><th>scores <span class="muted">(hover for the judge's reason)</span></th><th style="width:32%">answer</th></tr></thead><tbody>${shown.map((r) => `<tr class="${r.pass ? "pass" : "fail"}"><td>${r.pass ? '<span class="pill ok">pass</span>' : '<span class="pill warn">fail</span>'}<br><span class="muted">${esc(r.got || "")}</span></td><td>${esc(r.q)}${r.expected_output ? `<br><span class="muted">expects: ${esc(r.expected_output)}</span>` : ""}</td><td>${keys.map((k) => chip(k, (r.metrics || {})[k])).join("")}<div class="muted">${r.ms != null ? fmtMsE(r.ms) + " answer" : ""}${r.judge_ms ? ` · ${fmtMsE(r.judge_ms)} judge` : ""}${r.context_chunks != null ? ` · ${r.context_chunks} context block${r.context_chunks === 1 ? "" : "s"}` : ""} · ${fmtUsd(r.cost_usd)}${r.detail && !r.answer ? ` · ${esc(r.detail)}` : ""}</div></td><td><div class="q-ans">${md(r.answer || "")}</div>${(r.answer || "").length > 300 ? '<span class="q-more">show more</span>' : ""}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">${failedOnly ? "nothing failed" : "no rows"}</td></tr>`}</tbody></table>`;
  el.querySelectorAll(".q-more").forEach((m) => m.addEventListener("click", () => { const a = m.previousElementSibling; a.classList.toggle("open"); m.textContent = a.classList.contains("open") ? "show less" : "show more"; }));
}
async function showRun(id, switchTab = true) {
  const run = await api(`/evals/runs/${id}`); EV.results[run.set] = run; EV.selected = id; renderRun(run);
  $$("#ev-runs .run").forEach((el) => el.classList.toggle("sel", el.dataset.id === id));
  if (switchTab) { $(`.subtabs button[data-epane=${run.set}]`).click(); $(`#ev-result-${run.set}-panel`).scrollIntoView({ behavior: "smooth", block: "start" }); }
}

// ---- history + trend ----
async function loadEvalRuns() {
  try { EV.runs = await api("/evals/runs"); } catch { return; }
  const filter = $("#ev-runs-filter").value; const runs = EV.runs.filter((r) => !filter || r.set === filter);
  $("#ev-runs-count").textContent = `${EV.runs.length} runs`;
  const box = $("#ev-runs");
  box.innerHTML = runs.length ? runs.map((r) => { const s = r.summary || {}; const score = s.accuracy ?? s.pass_rate; const ms = r.finished_at && r.started_at ? new Date(r.finished_at) - new Date(r.started_at) : null;
    return `<div class="run ${EV.selected === r.id ? "sel" : ""}" data-id="${r.id}"><span class="pill ${score == null ? "info" : score >= 0.9 ? "ok" : score >= 0.6 ? "warn" : "err"}">${score == null ? esc(r.set) : Math.round(score * 100) + "%"}</span><span><span class="k">${esc(SET_LABEL[r.set] || r.set)}</span> ${s.skill ? esc(s.skill) : ""}<br><span class="s">${esc(s.models ? s.models.join(" vs ") : `${s.passed ?? 0}/${s.questions ?? 0} passed · ${fmtUsd(s.total_cost_usd)}${s.judge_model ? ` · judge ${s.judge_model}` : ""}`)}</span></span><span class="s">${new Date(r.started_at).toLocaleString()}<br>${ms != null ? fmtMsE(ms) : ""}</span><span class="acts"><a class="btn-secondary" href="/evals/runs/${r.id}.xlsx" title="Export this run as .xlsx">xlsx</a><button class="btn-danger del" title="Delete run">✕</button></span></div>`; }).join("") : "no runs yet";
  box.querySelectorAll(".run").forEach((el) => el.addEventListener("click", (e) => { if (e.target.closest(".acts")) return; showRun(el.dataset.id); }));
  box.querySelectorAll(".del").forEach((b) => b.addEventListener("click", async (e) => { e.stopPropagation(); const id = b.closest(".run").dataset.id; if (!confirm("Delete this run?")) return; await api(`/evals/runs/${id}`, { method: "DELETE" }); loadEvalRuns(); }));
  const trend = (set, label) => { const xs = EV.runs.filter((r) => r.set === set).slice(0, 12).reverse(); if (!xs.length) return ""; return `<div class="t">${label}<div class="bars" title="${xs.map((r) => Math.round(((r.summary || {}).accuracy ?? (r.summary || {}).pass_rate ?? 0) * 100) + "%").join(" · ")}">${xs.map((r, i) => `<i class="${i === xs.length - 1 ? "last" : ""}" style="height:${Math.max(6, Math.round(((r.summary || {}).accuracy ?? (r.summary || {}).pass_rate ?? 0) * 100))}%"></i>`).join("")}</div></div>`; };
  $("#ev-trend").innerHTML = trend("routing", "routing accuracy") + trend("rag", "grounded pass rate") + trend("quality", "quality pass rate");
}
$("#ev-runs-filter").addEventListener("change", loadEvalRuns);
