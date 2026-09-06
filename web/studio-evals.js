// Evals tab: editable question sets, routing / grounded runs, model comparison, run history.
const evState = { routing: [], rag: [] };

function rowInput(v, cls, ph = "") { return `<input class="${cls}" value="${esc(v ?? "")}" placeholder="${esc(ph)}" style="width:100%">`; }
function renderQuestions(set) {
  const tb = $(`#ev-${set} tbody`); tb.innerHTML = "";
  for (const [i, c] of evState[set].entries()) {
    const tr = document.createElement("tr");
    tr.innerHTML = set === "routing"
      ? `<td>${rowInput(c.q, "q")}</td><td>${rowInput(c.skill, "skill", "credit-card | offtopic")}</td><td><button class="btn-danger rm">✕</button></td>`
      : `<td>${rowInput(c.q, "q")}</td><td>${rowInput(c.skill, "skill", "auto")}</td><td>${rowInput((c.expect || []).join(", "), "expect")}</td><td><input type="checkbox" class="src" ${c.require_source !== false ? "checked" : ""}></td><td><button class="btn-danger rm">✕</button></td>`;
    tr.querySelector(".rm").addEventListener("click", () => { evState[set].splice(i, 1); renderQuestions(set); });
    tb.appendChild(tr);
  }
}
function collect(set) {
  return Array.from($(`#ev-${set} tbody`).querySelectorAll("tr")).map((tr) => set === "routing"
    ? { q: tr.querySelector(".q").value.trim(), skill: tr.querySelector(".skill").value.trim() }
    : { q: tr.querySelector(".q").value.trim(), skill: tr.querySelector(".skill").value.trim() || null, expect: tr.querySelector(".expect").value.split(",").map((x) => x.trim()).filter(Boolean), require_source: tr.querySelector(".src").checked }).filter((c) => c.q);
}
async function loadEvals() {
  for (const set of ["routing", "rag"]) { try { evState[set] = await api(`/evals/${set}`); } catch { evState[set] = []; } renderQuestions(set); }
  const sk = await api("/skills?remote=false"); $("#cmp-skill").innerHTML = sk.map((s) => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join("");
  await loadModels(); $("#cmp-models").innerHTML = S.models.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
  loadRuns();
}
S.loaders.evals = loadEvals;
for (const set of ["routing", "rag"]) {
  $(`#ev-add-${set}`).addEventListener("click", () => { evState[set] = collect(set); evState[set].push(set === "routing" ? { q: "", skill: "" } : { q: "", skill: null, expect: [], require_source: true }); renderQuestions(set); });
  $(`#ev-save-${set}`).addEventListener("click", async () => { evState[set] = collect(set); try { await api(`/evals/${set}`, json(evState[set], "PUT")); $(`#ev-status-${set}`).textContent = `saved ${evState[set].length} questions`; } catch (e) { $(`#ev-status-${set}`).textContent = e.message; } });
  $(`#ev-run-${set}`).addEventListener("click", async () => {
    evState[set] = collect(set); await api(`/evals/${set}`, json(evState[set], "PUT")).catch(() => {});
    const j = await api(`/evals/run?set=${set}`, { method: "POST" });
    pollJob(j.job_id, $("#ev-log"), $(`#ev-status-${set}`), (job) => { if (job.result) renderRun(job.result, $(`#ev-result-${set}`)); loadRuns(); });
  });
}
function renderRun(run, el) {
  const sm = run.summary || {}; const rows = run.rows || [];
  const head = run.set === "compare" ? `<tr><th>question</th>${(sm.models || []).map((m) => `<th>${esc(m)}</th>`).join("")}</tr>` : `<tr><th></th><th>question</th><th>expected</th><th>got</th><th>conf</th><th>ms</th><th>cost</th></tr>`;
  const body = run.set === "compare"
    ? rows.map((r) => `<tr><td>${esc(r.q)}</td>${(sm.models || []).map((m) => { const x = (r.by_model || {})[m] || {}; return `<td><div class="cmp-a">${md(x.text || x.error || "")}</div><div class="muted">${x.ms ? (x.ms / 1000).toFixed(1) + " s" : ""} · ${fmtUsd(x.cost_usd)} · ${x.input_tokens || 0} in / ${x.output_tokens || 0} out${x.retrieval_calls ? ` · ${x.retrieval_calls} retrieval` : ""}</div></td>`; }).join("")}</tr>`).join("")
    : rows.map((r) => `<tr><td>${r.pass ? '<span class="pill ok">pass</span>' : '<span class="pill warn">fail</span>'}</td><td>${esc(r.q)}</td><td>${esc(r.expected)}</td><td>${esc(r.got)}${r.detail ? `<br><span class="muted">${esc(r.detail)}</span>` : ""}</td><td>${r.confidence != null ? Math.round(r.confidence * 100) + "%" : ""}</td><td>${r.ms ?? ""}</td><td>${fmtUsd(r.cost_usd)}</td></tr>`).join("");
  el.innerHTML = `<div class="cards">${Object.entries(sm).filter(([k]) => !["models", "rows"].includes(k)).map(([k, v]) => `<div class="card"><div class="v">${esc(typeof v === "number" ? (k.includes("cost") ? fmtUsd(v) : k.includes("accuracy") || k.includes("rate") ? Math.round(v * 100) + "%" : v) : String(v))}</div><div class="k">${esc(k)}</div></div>`).join("")}</div><table class="list"><thead>${head}</thead><tbody>${body}</tbody></table>`;
}
$("#cmp-run").addEventListener("click", async () => {
  const models = Array.from($("#cmp-models").selectedOptions).map((o) => o.value); const questions = $("#cmp-qs").value.split("\n").map((x) => x.trim()).filter(Boolean);
  if (models.length < 2 || !questions.length) { $("#cmp-status").textContent = "pick at least 2 models and 1 question"; return; }
  const j = await api("/evals/compare", json({ skill: $("#cmp-skill").value, models, questions }));
  pollJob(j.job_id, $("#ev-log"), $("#cmp-status"), (job) => { if (job.result) renderRun(job.result, $("#cmp-result")); loadRuns(); });
});
async function loadRuns() {
  try { const runs = await api("/evals/runs"); const tb = $("#ev-runs tbody"); tb.innerHTML = "";
    for (const r of runs) { const tr = document.createElement("tr"); tr.innerHTML = `<td>${new Date(r.started_at).toLocaleString()}</td><td>${esc(r.set)}</td><td>${esc(Object.entries(r.summary || {}).filter(([k]) => !["models"].includes(k)).map(([k, v]) => `${k}=${typeof v === "number" ? +v.toFixed(4) : v}`).join(" · "))}</td><td><button class="btn-secondary">view</button></td>`; tr.querySelector("button").addEventListener("click", async () => renderRun(await api(`/evals/runs/${r.id}`), $("#ev-run-detail"))); tb.appendChild(tr); }
  } catch {}
}
S.loaders["epane-runs"] = loadRuns;
