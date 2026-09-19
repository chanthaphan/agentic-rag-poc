// Settings tab: usage & prices, base rules, runtime settings, export/import bundle.
async function loadUsage() {
  const st = await api("/sessions/stats");
  $("#us-cards").innerHTML = [["sessions", st.sessions], ["answers", st.answers], ["input tokens", st.input_tokens.toLocaleString()], ["output tokens", st.output_tokens.toLocaleString()],
    ["total cost", fmtUsd(st.cost_usd)], ["avg cost / answer", fmtUsd(st.avg_cost_usd)], ["avg latency", `${(st.avg_total_ms / 1000).toFixed(1)} s`],
    ["per skill", Object.entries(st.per_skill).map(([k, v]) => `${k}: ${v}`).join(", ") || "-"]].map(([k, v]) => `<div class="card"><div class="v">${esc(String(v))}</div><div class="k">${esc(k)}</div></div>`).join("");
  $("#us-days tbody").innerHTML = (st.per_day || []).map((d) => `<tr><td>${esc(d.day)}</td><td>${d.answers}</td><td>${d.input_tokens.toLocaleString()}</td><td>${fmtUsd(d.cost_usd)}</td></tr>`).join("");
  const pr = await api("/app/pricing"); $("#pr-retrieval").value = pr.retrieval_per_call || 0;
  $("#pr-table tbody").innerHTML = "";
  for (const [name, row] of Object.entries(pr.models)) addPriceRow(name, row);
}
function addPriceRow(name = "", row = {}) {
  const tr = document.createElement("tr");
  const cell = (cls, v) => `<td><input class="${cls}" type="number" step="0.01" value="${v ?? 0}" style="width:82px"></td>`;
  tr.innerHTML = `<td><input class="pr-name" value="${esc(name)}" ${name === "default" ? "readonly" : ""} style="width:200px"></td>`
    + cell("pr-in", row.input) + cell("pr-cached", row.cached_input) + cell("pr-out", row.output)
    + cell("pr-ain", row.audio_input) + cell("pr-acached", row.cached_audio_input) + cell("pr-aout", row.audio_output)
    + `<td>${name === "default" ? "" : '<button class="btn-danger pr-del">remove</button>'}</td>`;
  tr.querySelector(".pr-del")?.addEventListener("click", () => tr.remove());
  $("#pr-table tbody").appendChild(tr);
}
$("#pr-add").addEventListener("click", () => addPriceRow());
$("#pr-save").addEventListener("click", async () => {
  const models = {};
  for (const tr of $("#pr-table tbody").querySelectorAll("tr")) { const n = tr.querySelector(".pr-name").value.trim(); if (!n) continue; models[n] = { input: +tr.querySelector(".pr-in").value, cached_input: +tr.querySelector(".pr-cached").value, output: +tr.querySelector(".pr-out").value,
      audio_input: +tr.querySelector(".pr-ain").value, cached_audio_input: +tr.querySelector(".pr-acached").value, audio_output: +tr.querySelector(".pr-aout").value }; }
  try { await api("/app/pricing", json({ currency: "USD", models, retrieval_per_call: +$("#pr-retrieval").value || 0 }, "PUT")); $("#pr-status").textContent = "saved; applies to new answers"; } catch (e) { $("#pr-status").textContent = e.message; }
});
// what each choice actually changes, said before anyone saves it
function llmNote() {
  const p = $("#rt-LLM_PROVIDER").value;
  const speech = p === "openai" ? "OpenAI too" : "the Azure account (a gateway and Claude have no realtime or audio models)";
  const needs = p === "compatible" ? "Base URL required. " : p === "azure" ? "Key optional: without one the app uses its signed-in identity. " : "";
  $("#llm-note").textContent = `${needs}Chat, routing and suggestions run on this service; speech mode and playback run on ${speech}.`;
}
$("#rt-LLM_PROVIDER").addEventListener("change", llmNote);
$("#llm-test").addEventListener("click", async () => {
  const out = $("#llm-test-status");
  out.textContent = "asking the model…";
  try {
    const r = await api("/app/llm/test", json({}, "POST"));
    out.textContent = r.ok
      ? `ok · ${r.service} · ${r.model} · ${r.elapsed_ms} ms · replied "${r.reply}" · speech on ${r.speech_on}`
      : `failed · ${r.service} · ${r.error}`;
  } catch (e) { out.textContent = e.message; }
});
S.loaders.settings = loadUsage; S.loaders["spane-usage"] = loadUsage;

// ---- base rules ----
S.loaders["spane-base"] = async () => { const b = await api("/skills/_base"); $("#base-body").value = b.body; };
async function saveBase() { const r = await api("/skills/_base", json({ body: $("#base-body").value }, "PUT")); $("#base-status").textContent = "saved"; return r; }
$("#base-save").addEventListener("click", () => saveBase().catch((e) => { $("#base-status").textContent = e.message; }));
$("#base-save-sync").addEventListener("click", async () => { try { await saveBase(); const j = await api("/skills/sync", { method: "POST" }); pollJob(j.job_id, $("#st-log"), $("#base-status")); } catch (e) { $("#base-status").textContent = e.message; } });

// ---- runtime settings ----
// The voices the realtime and audio models speak with; cedar and marin are the newer, warmer pair.
const VOICES = ["alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse"];
const TRANSCRIBERS = ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"];
const READERS = ["gpt-audio-1.5", "gpt-4o-mini-tts", ""];
const RT_KEYS = ["SUGGESTIONS_MODE", "SUGGESTIONS_MODEL", "ROUTER_MODEL", "DEFAULT_CHAT_MODEL", "KB_REASONING_EFFORT", "KB_LLM_DEPLOYMENT", "ASSISTANT_NAME", "ASSISTANT_NAME_EN", "APP_USER_NAME", "APP_USER_INITIALS", "ORCHESTRATION_MODE", "CONCIERGE_MODEL", "HISTORY_TURNS", "JUDGE_MODEL", "ASSISTANT_GENDER", "REALTIME_DEPLOYMENT", "REALTIME_VOICE", "REALTIME_TRANSCRIBE_MODEL", "REALTIME_AVATAR_URL", "TTS_DEPLOYMENT", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY"];
S.loaders["spane-runtime"] = async () => {
  const s = await api("/app/settings"); await loadModels();
  for (const k of ["ROUTER_MODEL", "DEFAULT_CHAT_MODEL", "KB_LLM_DEPLOYMENT", "JUDGE_MODEL", "SUGGESTIONS_MODEL"]) fillModelSelect($(`#rt-${k}`), s.effective[k]);
  $("#rt-SUGGESTIONS_MODE").value = s.effective.SUGGESTIONS_MODE || "dynamic";
  fillModelSelect($("#rt-CONCIERGE_MODEL"), s.effective.CONCIERGE_MODEL || s.effective.DEFAULT_CHAT_MODEL); $("#rt-ORCHESTRATION_MODE").value = s.effective.ORCHESTRATION_MODE || "router"; $("#rt-HISTORY_TURNS").value = s.effective.HISTORY_TURNS ?? 6;
  $("#rt-KB_REASONING_EFFORT").value = s.effective.KB_REASONING_EFFORT; $("#rt-ASSISTANT_NAME").value = s.effective.ASSISTANT_NAME; $("#rt-ASSISTANT_NAME_EN").value = s.effective.ASSISTANT_NAME_EN || ""; $("#rt-APP_USER_NAME").value = s.effective.APP_USER_NAME; $("#rt-APP_USER_INITIALS").value = s.effective.APP_USER_INITIALS;
  $("#rt-ASSISTANT_GENDER").value = s.effective.ASSISTANT_GENDER || "male";
  fillSelect($("#rt-REALTIME_VOICE"), VOICES.map((n) => ({ name: n })), s.effective.REALTIME_VOICE);
  fillSelect($("#rt-REALTIME_TRANSCRIBE_MODEL"), TRANSCRIBERS.map((n) => ({ name: n })), s.effective.REALTIME_TRANSCRIBE_MODEL);
  fillSelect($("#rt-TTS_DEPLOYMENT"), READERS.map((n) => ({ name: n })), s.effective.TTS_DEPLOYMENT);
  $("#rt-REALTIME_AVATAR_URL").value = s.effective.REALTIME_AVATAR_URL || "";
  try { fillSelect($("#rt-REALTIME_DEPLOYMENT"), await api("/app/models?kind=realtime"), s.effective.REALTIME_DEPLOYMENT); }
  catch { fillSelect($("#rt-REALTIME_DEPLOYMENT"), [], s.effective.REALTIME_DEPLOYMENT); }
  $("#rt-LLM_PROVIDER").value = s.effective.LLM_PROVIDER || "azure";
  llmNote();
  $("#rt-LLM_BASE_URL").value = s.effective.LLM_BASE_URL || "";
  $("#rt-LLM_API_KEY").value = s.effective.LLM_API_KEY || "";   // dots when a key is bound, empty when none is
  $("#rt-status").textContent = Object.keys(s.overlay).length ? `overrides active: ${Object.keys(s.overlay).join(", ")}` : "no overrides (values from .env)";
};
async function saveRuntime() { const data = {}; for (const k of RT_KEYS) data[k] = $(`#rt-${k}`).value; const r = await api("/app/settings", json(data, "PUT")); $("#rt-status").textContent = r.note; return r; }
$("#rt-save").addEventListener("click", () => saveRuntime().catch((e) => { $("#rt-status").textContent = e.message; }));
$("#rt-save-sync").addEventListener("click", async () => { try { await saveRuntime(); const j = await api("/skills/sync", { method: "POST" }); pollJob(j.job_id, $("#st-log"), $("#rt-status")); } catch (e) { $("#rt-status").textContent = e.message; } });

// ---- bundle export / import ----
const fmtBytes = (b) => b < 1e6 ? `${Math.round(b / 1e3)} KB` : `${(b / 1e6).toFixed(1)} MB`;
const BD = { file: null, info: null };
function bdExportUrl() {
  const parts = Array.from($$(".bd-part:checked")).map((x) => x.value);
  return `/bundle.zip?parts=${encodeURIComponent(parts.join(",") || "skills")}&pdfs=${$("#bd-pdfs").checked}`;
}
function bdRefreshExport() { $("#bd-download").href = bdExportUrl(); $("#bd-pdfs").disabled = !$(".bd-part[value=knowledge]").checked; }
$$(".bd-part, #bd-pdfs").forEach((x) => x.addEventListener("change", bdRefreshExport)); bdRefreshExport();

$("#bd-file").addEventListener("change", async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return;
  BD.file = f; $("#bd-filename").textContent = `${f.name} · ${fmtBytes(f.size)}`; $("#bd-status").textContent = "inspecting…"; $("#bd-preview").hidden = true; $("#bd-job").hidden = true;
  const fd = new FormData(); fd.append("file", f);
  try {
    const info = await api("/bundle/inspect", { method: "POST", body: fd }); BD.info = info;
    const label = { skills: "Skills", rules: "Responsible Lending rules", knowledge: "Knowledge", evals: "Eval questions", config: "Prices & settings" };
    $("#bd-parts").innerHTML = Object.entries(info.parts).map(([p, s]) => `<tr><td>${label[p]}</td><td>${s.files}</td><td>${s.new ? `<span class="pill ok">${s.new}</span>` : ""}</td><td>${s.changed ? `<span class="pill warn">${s.changed}</span>` : ""}</td><td class="muted">${s.same || ""}</td><td class="muted">${s.files ? fmtBytes(s.bytes) : ""}</td><td><input type="checkbox" class="bd-imp" value="${p}" ${s.files ? "checked" : "disabled"}></td></tr>`).join("");
    const cats = Object.entries(info.categories).map(([c, n]) => `${c} (${n})`).join(", ");
    $("#bd-detail").innerHTML = `${info.skills.length ? `skills: ${esc(info.skills.join(", "))}` : "no skills"} · ${cats ? `knowledge: ${esc(cats)}${info.pdfs ? ` · ${info.pdfs} PDF` : ""}` : "no knowledge"}${info.skipped.length ? ` · <span class="pill warn">${info.skipped.length} unknown file(s) skipped</span>` : ""}`;
    $("#bd-preview").hidden = false; $("#bd-status").textContent = "";
  } catch (err) { $("#bd-status").textContent = err.message; }
});
$("#bd-import").addEventListener("click", async () => {
  if (!BD.file) return;
  const parts = Array.from($$(".bd-imp:checked")).map((x) => x.value); if (!parts.length) { $("#bd-status").textContent = "tick at least one part"; return; }
  const mode = $("input[name=bd-mode]:checked").value;
  if (mode === "replace" && !confirm(`Replace mode deletes the existing ${parts.filter((p) => p === "skills" || p === "knowledge").join(" and ") || "selected"} folders before writing the bundle. Continue?`)) return;
  const fd = new FormData(); fd.append("file", BD.file); $("#bd-status").textContent = "importing…"; $("#bd-import").disabled = true;
  try {
    const r = await api(`/bundle?mode=${mode}&parts=${encodeURIComponent(parts.join(","))}&ingest=${$("#bd-ingest").checked}&sync=${$("#bd-sync").checked}`, { method: "POST", body: fd });
    const c = r.counts; $("#bd-status").textContent = `written: ${c.skills} skill files, ${c.knowledge} knowledge files, ${c.evals} eval files, ${c.config} config · changed skills: ${r.changed_skills.join(", ") || "none"} · changed spaces: ${r.changed_categories.join(", ") || "none"}`;
    $("#bd-job").hidden = false; $("#bd-log").textContent = (r.log || []).join("\n");
    if (r.job_id) {
      $("#bd-job-state").textContent = "running follow-up job"; $("#bd-bar").className = "fill indet";
      const tick = async () => { const j = await api(`/jobs/${r.job_id}`); $("#bd-log").textContent = (j.log || []).join("\n"); $("#bd-log").scrollTop = 1e9; const pr = j.progress || {}; $("#bd-job-msg").textContent = pr.message ? `${pr.phase}: ${pr.message}` : (pr.phase || "");
        if (j.status !== "running") { $("#bd-job-state").textContent = j.status === "done" ? "ingest + sync finished" : "follow-up job failed"; $("#bd-bar").className = "fill " + (j.status === "done" ? "ok" : "err"); $("#bd-bar").style.width = "100%"; S.skillsCache = []; return; }
        setTimeout(tick, 900); };
      tick();
    } else { $("#bd-job-state").textContent = "done (no ingest or sync requested, or nothing changed)"; $("#bd-bar").className = "fill ok"; $("#bd-bar").style.width = "100%"; }
  } catch (e) { $("#bd-status").textContent = e.message; }
  $("#bd-import").disabled = false;
});

// ---- access list (admins) ----
async function loadAccess() {
  try {
    const d = await api("/access"); const tb = $("#ac-table tbody"); tb.innerHTML = "";
    for (const u of d.users) {
      const me = u.email === (d.me.email || "").toLowerCase(); const seeded = u.email in (d.seeded || {});
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(u.email)}${me ? ' <span class="pill info">you</span>' : ""}</td><td><span class="pill ${u.role === "admin" ? "ok" : u.role === "external" ? "warn" : ""}">${esc(u.role)}</span></td><td>${esc(u.name)}</td><td class="muted">${esc(u.added_by)}</td><td class="muted">${u.at ? new Date(u.at).toLocaleDateString() : ""}</td><td>${me || seeded ? "" : `<button class="btn-danger rm" data-email="${esc(u.email)}">remove</button>`}</td>`;
      tb.appendChild(tr);
    }
    if (!d.users.length) tb.innerHTML = `<tr><td colspan="6" class="muted">nobody yet: until someone is added, the shared password is the only gate</td></tr>`;
    tb.querySelectorAll(".rm").forEach((b) => b.addEventListener("click", async () => { if (!confirm(`Remove ${b.dataset.email} from Studio?`)) return; try { await api(`/access/${encodeURIComponent(b.dataset.email)}`, { method: "DELETE" }); loadAccess(); } catch (e) { $("#ac-status").textContent = e.message; } }));
    $("#ac-status").textContent = `${d.users.length} account(s)`;
    const seededList = Object.entries(d.seeded || {}).map(([e, v]) => `${e} (${v})`);
    $("#ac-seed").textContent = seededList.length ? `Seeded from the environment (change the variable to remove them): ${seededList.join(", ")}` : "";
  } catch (e) { $("#ac-status").textContent = e.message; }
}
S.loaders["spane-access"] = loadAccess;
$("#ac-add").addEventListener("click", async () => {
  const email = $("#ac-email").value.trim(); if (!email) { $("#ac-add-status").textContent = "enter an email"; return; }
  try { const r = await api("/access", json({ email, role: $("#ac-role").value, name: $("#ac-name").value })); $("#ac-add-status").textContent = `${r.email} is now ${r.role}`; $("#ac-email").value = ""; $("#ac-name").value = ""; loadAccess(); } catch (e) { $("#ac-add-status").textContent = e.message; }
});
$("#ac-email").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#ac-add").click(); });
