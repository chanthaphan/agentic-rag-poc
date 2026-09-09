// Settings > Responsible lending: the rule pack, which agents carry it, and a dry run of the answer-time guard.
const RLC = { compliant: "ok", non_compliant: "warn", undefined: "info", not_applicable: "" };
const CHECK_LABEL = { required_phrase: "required wording", prohibited_phrase: "prohibited wording", required_pattern: "required figures", judgement: "judgement (agent + review)" };

S.loaders["spane-lending"] = async () => {
  let d;
  try { d = await api("/rules"); } catch (e) { $("#rl-status").textContent = e.message; return; }
  const active = d.rules.filter((r) => r.status === "active").length;
  $("#rl-intro").innerHTML = `<b>${esc(d.pack.name)}</b> — ${esc(d.pack.description)}<br>${d.rules.length} rule(s), ${active} active, from ${d.pack.sources.map(esc).join("; ")}.
    Rules are files in <code>rules/${esc(d.pack.id)}/</code>: they are compiled into the concierge and specialist instructions on the next sync, and every answer is checked against them before it is sent.`;
  $("#rl-table tbody").innerHTML = d.rules.map((r) => `<tr class="${r.status === "active" ? "" : "dim"}">
    <td class="muted">${esc(r.clause)}</td>
    <td><b>${esc(r.title)}</b><div class="muted">${esc(r.system_rule.slice(0, 180))}${r.system_rule.length > 180 ? "…" : ""}</div>
      ${r.errors.length ? `<span class="pill warn">${esc(r.errors.join("; "))}</span>` : ""}</td>
    <td class="muted">${r.product_names.map(esc).join("<br>")}</td>
    <td><span class="pill ${r.check === "judgement" ? "info" : "ok"}">${esc(CHECK_LABEL[r.check] || r.check)}</span>
      <div class="muted">${r.enforcement === "append" ? "missing wording is added to the answer" : "reported on the turn"} · ${esc(r.severity)}</div></td>
    <td class="muted">${r.skills.map(esc).join(", ") || '<span class="pill warn">no agent</span>'}</td>
    <td>${esc(r.status)}</td></tr>`).join("");
  $("#rl-products tbody").innerHTML = d.products.map((p) => `<tr><td>${esc(p.name)}<div class="muted">${esc(p.id)}</div></td>
    <td class="muted">${p.skills.map(esc).join(", ") || '<span class="pill warn">none</span>'}${p.unknown_skills.length ? ` <span class="pill warn">unknown: ${esc(p.unknown_skills.join(", "))}</span>` : ""}</td>
    <td>${p.rules.length}</td></tr>`).join("");
  const sel = $("#rl-try-skill");
  const skills = Array.from(new Set(d.products.flatMap((p) => p.skills)));
  sel.innerHTML = `<option value="">(detect from the text)</option>` + skills.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  $("#rl-status").textContent = "";
};

$("#rl-try-run").addEventListener("click", async () => {
  const text = $("#rl-try").value.trim();
  if (!text) return;
  $("#rl-try-out").innerHTML = '<span class="muted">checking…</span>';
  try {
    const r = await api("/rules/check", json({ text, skill: $("#rl-try-skill").value, language: /[฀-๿]/.test(text) ? "th" : "en" }));
    const rep = r.report || {};
    const rows = (rep.findings || []).map((f) => `<li><span class="pill ${RLC[f.verdict] || ""}">${esc(f.verdict)}</span> ${esc(f.clause)} ${esc(f.title)}${f.detail ? ` <span class="muted">— ${esc(f.detail)}</span>` : ""}</li>`).join("");
    $("#rl-try-out").innerHTML = `<div class="muted">products: ${(rep.product_names || []).map(esc).join(", ") || "none detected, so no rule applies"}</div>
      <ul class="dp-src">${rows || '<li class="muted">no findings</li>'}</ul>
      ${r.text !== text ? `<div class="muted">answer after the guard:</div><pre class="log">${esc(r.text)}</pre>` : ""}`;
  } catch (e) { $("#rl-try-out").innerHTML = `<span class="pill warn">${esc(e.message)}</span>`; }
});

$("#rl-file").addEventListener("change", async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return;
  $("#rl-status").textContent = `inspecting ${f.name}…`;
  const send = async (dry) => { const fd = new FormData(); fd.append("file", f); return api(`/rules/import?dry_run=${dry}`, { method: "POST", body: fd }); };
  try {
    const pre = await send(true);
    const msg = `${pre.rows} row(s): ${pre.created.length} new, ${pre.updated.length} updated, ${pre.unchanged.length} unchanged.${pre.warnings.length ? `\n\n${pre.warnings.join("\n")}` : ""}\n\nImport now? How each rule is checked is preserved; the legal columns are overwritten.`;
    if (!confirm(msg)) { $("#rl-status").textContent = "import cancelled"; return; }
    const r = await send(false);
    $("#rl-status").textContent = `imported: ${r.created.length} new, ${r.updated.length} updated${r.new_products.length ? `, ${r.new_products.length} new product family — give it an id and skills in PACK.md` : ""}. Sync the agents to push the change.`;
    S.loaders["spane-lending"]();
  } catch (err) { $("#rl-status").textContent = err.message; }
});
