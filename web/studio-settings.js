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
  tr.innerHTML = `<td><input class="pr-name" value="${esc(name)}" ${name === "default" ? "readonly" : ""} style="width:220px"></td><td><input class="pr-in" type="number" step="0.01" value="${row.input ?? 0}" style="width:90px"></td><td><input class="pr-cached" type="number" step="0.01" value="${row.cached_input ?? 0}" style="width:90px"></td><td><input class="pr-out" type="number" step="0.01" value="${row.output ?? 0}" style="width:90px"></td><td>${name === "default" ? "" : '<button class="btn-danger pr-del">remove</button>'}</td>`;
  tr.querySelector(".pr-del")?.addEventListener("click", () => tr.remove());
  $("#pr-table tbody").appendChild(tr);
}
$("#pr-add").addEventListener("click", () => addPriceRow());
$("#pr-save").addEventListener("click", async () => {
  const models = {};
  for (const tr of $("#pr-table tbody").querySelectorAll("tr")) { const n = tr.querySelector(".pr-name").value.trim(); if (!n) continue; models[n] = { input: +tr.querySelector(".pr-in").value, cached_input: +tr.querySelector(".pr-cached").value, output: +tr.querySelector(".pr-out").value }; }
  try { await api("/app/pricing", json({ currency: "USD", models, retrieval_per_call: +$("#pr-retrieval").value || 0 }, "PUT")); $("#pr-status").textContent = "saved; applies to new answers"; } catch (e) { $("#pr-status").textContent = e.message; }
});
S.loaders.settings = loadUsage; S.loaders["spane-usage"] = loadUsage;

// ---- base rules ----
S.loaders["spane-base"] = async () => { const b = await api("/skills/_base"); $("#base-body").value = b.body; };
async function saveBase() { const r = await api("/skills/_base", json({ body: $("#base-body").value }, "PUT")); $("#base-status").textContent = "saved"; return r; }
$("#base-save").addEventListener("click", () => saveBase().catch((e) => { $("#base-status").textContent = e.message; }));
$("#base-save-sync").addEventListener("click", async () => { try { await saveBase(); const j = await api("/skills/sync", { method: "POST" }); pollJob(j.job_id, $("#st-log"), $("#base-status")); } catch (e) { $("#base-status").textContent = e.message; } });

// ---- runtime settings ----
const RT_KEYS = ["ROUTER_MODEL", "DEFAULT_CHAT_MODEL", "KB_REASONING_EFFORT", "KB_LLM_DEPLOYMENT", "ASSISTANT_NAME", "APP_USER_NAME", "APP_USER_INITIALS"];
S.loaders["spane-runtime"] = async () => {
  const s = await api("/app/settings"); await loadModels();
  for (const k of ["ROUTER_MODEL", "DEFAULT_CHAT_MODEL", "KB_LLM_DEPLOYMENT"]) fillModelSelect($(`#rt-${k}`), s.effective[k]);
  $("#rt-KB_REASONING_EFFORT").value = s.effective.KB_REASONING_EFFORT; $("#rt-ASSISTANT_NAME").value = s.effective.ASSISTANT_NAME; $("#rt-APP_USER_NAME").value = s.effective.APP_USER_NAME; $("#rt-APP_USER_INITIALS").value = s.effective.APP_USER_INITIALS;
  $("#rt-status").textContent = Object.keys(s.overlay).length ? `overrides active: ${Object.keys(s.overlay).join(", ")}` : "no overrides (values from .env)";
};
async function saveRuntime() { const data = {}; for (const k of RT_KEYS) data[k] = $(`#rt-${k}`).value; const r = await api("/app/settings", json(data, "PUT")); $("#rt-status").textContent = r.note; return r; }
$("#rt-save").addEventListener("click", () => saveRuntime().catch((e) => { $("#rt-status").textContent = e.message; }));
$("#rt-save-sync").addEventListener("click", async () => { try { await saveRuntime(); const j = await api("/skills/sync", { method: "POST" }); pollJob(j.job_id, $("#st-log"), $("#rt-status")); } catch (e) { $("#rt-status").textContent = e.message; } });

// ---- bundle ----
async function importBundle(file, mode) {
  const fd = new FormData(); fd.append("file", file);
  try { const r = await api(`/bundle?mode=${mode}`, { method: "POST", body: fd }); $("#bd-status").textContent = `imported: ${JSON.stringify(r.counts)}`; $("#bd-log").textContent = (r.log || []).join("\n"); } catch (e) { $("#bd-status").textContent = e.message; }
}
$("#bd-merge").addEventListener("change", (e) => { if (e.target.files[0]) importBundle(e.target.files[0], "merge"); e.target.value = ""; });
$("#bd-replace").addEventListener("change", (e) => { if (e.target.files[0] && confirm("Replace ALL skills and knowledge with the bundle contents?")) importBundle(e.target.files[0], "replace"); e.target.value = ""; });
