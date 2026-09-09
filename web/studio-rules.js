// Settings > Responsible lending: the rule pack, which agents carry it, editors for both, and a dry run of the guard.
const RLC = { compliant: "ok", non_compliant: "warn", undefined: "info", not_applicable: "" };
const CHECK_LABEL = { required_phrase: "required wording", prohibited_phrase: "prohibited wording", required_pattern: "required figures", judgement: "judgement (agent + review)" };
const RL = { pack: "mccs", rules: [], products: [], skills: [] };
const lines = (v) => (v || []).join("\n");
const split = (el) => el.value.split("\n").map((x) => x.trim()).filter(Boolean);
function boxes(host, values, checked) {
  host.innerHTML = values.map((v) => `<label class="chk"><input type="checkbox" value="${esc(v)}" ${checked.includes(v) ? "checked" : ""}> ${esc(v)}</label>`).join("") || `<span class="muted">none</span>`;
}
const picked = (host) => Array.from(host.querySelectorAll("input:checked")).map((x) => x.value);

S.loaders["spane-lending"] = async () => {
  let d;
  try { d = await api("/rules"); } catch (e) { $("#rl-status").textContent = e.message; return; }
  Object.assign(RL, { pack: d.pack.id, rules: d.rules, products: d.products, skills: d.skills || [] });
  const active = d.rules.filter((r) => r.status === "active").length;
  $("#rl-intro").innerHTML = `<b>${esc(d.pack.name)}</b> — ${esc(d.pack.description)}<br>${d.rules.length} rule(s), ${active} active, from ${d.pack.sources.map(esc).join("; ")}.
    Rules are files in <code>rules/${esc(d.pack.id)}/</code>: they are compiled into the concierge and specialist instructions on the next sync, and every answer is checked against them before it is sent.
    ${(d.pack_errors || []).map((e) => `<div><span class="pill warn">${esc(e)}</span></div>`).join("")}`;
  $("#rl-table tbody").innerHTML = d.rules.map((r) => `<tr class="${r.status === "active" ? "" : "dim"}">
    <td class="muted">${esc(r.clause)}</td>
    <td><b>${esc(r.title)}</b><div class="muted">${esc(r.system_rule.slice(0, 180))}${r.system_rule.length > 180 ? "…" : ""}</div>
      ${r.errors.length ? `<span class="pill warn">${esc(r.errors.join("; "))}</span>` : ""}</td>
    <td class="muted">${r.product_names.map(esc).join("<br>")}</td>
    <td><span class="pill ${r.check === "judgement" ? "info" : "ok"}">${esc(CHECK_LABEL[r.check] || r.check)}</span>
      <div class="muted">${r.enforcement === "append" ? "missing wording is added to the answer" : "reported on the turn"} · ${esc(r.severity)}</div>
      <div class="muted">${r.trigger === "mention" ? "on any mention" : "only when offering / recommending"}</div></td>
    <td class="muted">${r.skills.map(esc).join(", ") || '<span class="pill warn">no agent</span>'}</td>
    <td>${esc(r.status)}</td>
    <td><button class="btn-secondary rl-edit" data-id="${esc(r.id)}" data-admin>edit</button></td></tr>`).join("");
  $("#rl-products tbody").innerHTML = d.products.map((p) => `<tr><td>${esc(p.name)}<div class="muted">${esc(p.id)}</div></td>
    <td class="muted">${p.skills.map(esc).join(", ") || '<span class="pill warn">none: no agent carries these rules</span>'}${p.unknown_skills.length ? ` <span class="pill warn">unknown: ${esc(p.unknown_skills.join(", "))}</span>` : ""}</td>
    <td>${p.rules.length}</td>
    <td><button class="btn-secondary rl-edit-product" data-id="${esc(p.id)}" data-admin>edit</button></td></tr>`).join("");
  $$(".rl-edit").forEach((b) => b.addEventListener("click", () => openRule(RL.rules.find((r) => r.id === b.dataset.id))));
  $$(".rl-edit-product").forEach((b) => b.addEventListener("click", () => openProduct(RL.products.find((p) => p.id === b.dataset.id))));
  const sel = $("#rl-try-skill");
  const skills = Array.from(new Set(d.products.flatMap((p) => p.skills)));
  sel.innerHTML = `<option value="">(detect from the text)</option>` + skills.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  $("#rl-status").textContent = "";
};

// ---- product family: which skills carry its rules ----
function openProduct(p) {
  const isNew = !p;
  $("#dp-title").textContent = isNew ? "New product family" : `Product family: ${p.name}`;
  $("#dp-id").value = p ? p.id : ""; $("#dp-id").readOnly = !isNew;
  $("#dp-name").value = p ? p.name : "";
  $("#dp-aliases").value = p ? lines(p.aliases) : "";
  $("#dp-match").value = p ? lines(p.match) : "";
  boxes($("#dp-skills"), RL.skills, p ? p.skills : []);
  $("#dp-delete").hidden = isNew; $("#dp-status").textContent = "";
  $("#dlg-product").returnValue = ""; $("#dlg-product").showModal();
}
$("#dlg-product form").addEventListener("submit", async (e) => {
  const action = e.submitter && e.submitter.value, id = $("#dp-id").value.trim();
  if (action === "cancel" || !id) return;
  e.preventDefault();
  let msg = "";
  try {
    if (action === "delete") {
      if (!confirm(`Remove the product family "${$("#dp-name").value}"? Rules still using it must be changed first.`)) return;
      await api(`/rules/products/${encodeURIComponent(id)}?pack=${RL.pack}`, { method: "DELETE" });
      msg = `removed ${id}`;
    } else {
      await api(`/rules/products/${encodeURIComponent(id)}`, json({ pack: RL.pack, name: $("#dp-name").value, skills: picked($("#dp-skills")), aliases: split($("#dp-aliases")), match: split($("#dp-match")) }, "PUT"));
      msg = `saved ${id} — sync the agents so they pick the change up`;
    }
    $("#dlg-product").close();
    await S.loaders["spane-lending"]();
    $("#rl-status").textContent = msg;
  } catch (err) { $("#dp-status").textContent = err.message; }
});
$("#rl-new-product").addEventListener("click", () => openProduct(null));

// ---- rule ----
function openRule(r) {
  const isNew = !r;
  $("#dr-title").textContent = isNew ? "New rule" : `Rule: ${r.clause || r.id}`;
  const v = (sel, x) => { $(sel).value = x || ""; };
  $("#dr-id").value = r ? r.id : ""; $("#dr-id").readOnly = !isNew;
  v("#dr-clause", r && r.clause); v("#dr-title-in", r && r.title); v("#dr-regulation", r && r.regulation);
  v("#dr-status", r ? r.status : "active"); v("#dr-severity", r ? r.severity : "block"); v("#dr-trigger", r ? r.trigger : "promotion");
  v("#dr-check", r ? r.check : "judgement"); v("#dr-enforcement", r ? r.enforcement : "flag");
  v("#dr-phrases", r && lines(r.phrases)); v("#dr-patterns", r && lines(r.patterns)); v("#dr-applies", r && lines(r.applies_when));
  v("#dr-disc-th", r && (r.disclosure || {}).th); v("#dr-disc-en", r && (r.disclosure || {}).en);
  v("#dr-template", r && r.template); v("#dr-legal", r && r.legal_text); v("#dr-system", r && r.system_rule); v("#dr-note", r && r.assistant_note);
  boxes($("#dr-products"), RL.products.map((p) => p.id), r ? r.products : []);
  $("#dr-delete").hidden = isNew; $("#dr-msg").textContent = "";
  $("#dlg-rule").returnValue = ""; $("#dlg-rule").showModal();
}
$("#dlg-rule form").addEventListener("submit", async (e) => {
  const action = e.submitter && e.submitter.value, id = $("#dr-id").value.trim();
  if (action === "cancel" || !id) return;
  e.preventDefault();
  let msg = "";
  const disclosure = {};
  if ($("#dr-disc-th").value.trim()) disclosure.th = $("#dr-disc-th").value.trim();
  if ($("#dr-disc-en").value.trim()) disclosure.en = $("#dr-disc-en").value.trim();
  const body = {
    pack: RL.pack, id, clause: $("#dr-clause").value, title: $("#dr-title-in").value, regulation: $("#dr-regulation").value,
    products: picked($("#dr-products")), status: $("#dr-status").value, severity: $("#dr-severity").value, trigger: $("#dr-trigger").value,
    check: $("#dr-check").value, enforcement: $("#dr-enforcement").value,
    phrases: split($("#dr-phrases")), patterns: split($("#dr-patterns")), applies_when: split($("#dr-applies")),
    disclosure, template: $("#dr-template").value,
    legal_text: $("#dr-legal").value, system_rule: $("#dr-system").value, assistant_note: $("#dr-note").value,
  };
  try {
    if (action === "delete") {
      if (!confirm(`Delete the rule "${$("#dr-title-in").value}"? It stops applying to every answer.`)) return;
      await api(`/rules/${encodeURIComponent(id)}?pack=${RL.pack}`, { method: "DELETE" });
      msg = `deleted ${id}`;
    } else if (RL.rules.some((x) => x.id === id)) {
      await api(`/rules/${encodeURIComponent(id)}`, json(body, "PUT"));
      msg = `saved ${id} — sync the agents so they pick the change up`;
    } else {
      await api("/rules", json(body));
      msg = `created ${id} — sync the agents so they pick it up`;
    }
    $("#dlg-rule").close();
    await S.loaders["spane-lending"]();
    $("#rl-status").textContent = msg;
  } catch (err) { $("#dr-msg").textContent = err.message; }
});
$("#rl-new").addEventListener("click", () => openRule(null));

// ---- dry run of the answer-time guard ----
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

// ---- import / export the MCCS sheet ----
$("#rl-file").addEventListener("change", async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return;
  $("#rl-status").textContent = `inspecting ${f.name}…`;
  const send = async (dry) => { const fd = new FormData(); fd.append("file", f); return api(`/rules/import?dry_run=${dry}&pack=${RL.pack}`, { method: "POST", body: fd }); };
  try {
    const pre = await send(true);
    const msg = `${pre.rows} row(s): ${pre.created.length} new, ${pre.updated.length} updated, ${pre.unchanged.length} unchanged.${pre.warnings.length ? `\n\n${pre.warnings.join("\n")}` : ""}\n\nImport now? How each rule is checked is preserved; the legal columns are overwritten.`;
    if (!confirm(msg)) { $("#rl-status").textContent = "import cancelled"; return; }
    const r = await send(false);
    $("#rl-status").textContent = `imported: ${r.created.length} new, ${r.updated.length} updated${r.new_products.length ? `, ${r.new_products.length} new product family — open it to set its id and skills` : ""}. Sync the agents to push the change.`;
    S.loaders["spane-lending"]();
  } catch (err) { $("#rl-status").textContent = err.message; }
});
