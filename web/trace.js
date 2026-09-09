// Shared trace-card rendering (mobile app + Studio). Requires nothing else.
const TR = (() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmtUsd = (x) => x == null ? "-" : (x < 0.01 ? `$${x.toFixed(4)}` : `$${x.toFixed(3)}`);
  const fmtK = (n) => (n || 0) >= 1000 ? `${((n || 0) / 1000).toFixed(1)}k` : String(n || 0);
  function bar(segments, total) {
  // segments: [{label, value, cls}] -> proportional stacked bar
  const t = total || segments.reduce((a, s) => a + (s.value || 0), 0) || 1;
  return `<div class="bar">${segments.map((s) => `<span class="seg ${s.cls}" style="width:${Math.max(2, (100 * (s.value || 0)) / t).toFixed(1)}%" title="${esc(s.label)}: ${esc(String(s.value ?? "-"))}"></span>`).join("")}</div>
    <div class="legend">${segments.map((s) => `<span><i class="${s.cls}"></i>${esc(s.label)} ${esc(s.text ?? String(s.value ?? "-"))}</span>`).join("")}</div>`;
}
  function traceCard(t, q) {
  const tr = t.trace || {}; const tm = tr.timings_ms || {}; const u = tr.usage || {}; const rt = tr.retrieval || {}; const cost = tr.cost || {};
  const conf = t.confidence == null ? "" : ` ${Math.round(t.confidence * 100)}%`;
  let html = `<div class="tc"><div class="tc-q">${esc(q)}</div>
    <div class="tc-head"><span class="dp-tag ${t.skill_id === "offtopic" ? "warn" : "ok"}">${esc(t.skill_id || "?")}${conf}</span>${t.language ? `<span class="dp-tag">${esc(t.language)}</span>` : ""}${t.agent_name ? `<span class="dp-tag dim">${esc(t.agent_name)}</span>` : ""}${cost.total_usd != null ? `<span class="dp-tag cost">${fmtUsd(cost.total_usd)}</span>` : ""}${tm.total != null ? `<span class="dp-tag dim">${(tm.total / 1000).toFixed(1)} s</span>` : ""}</div>`;
  if (t.route_reason) html += `<div class="tc-row"><span class="k">why</span><span class="v">${esc(t.route_reason)}</span></div>`;
  if (tr.handoff) {
    const ho = tr.handoff; const sp = u.specialist; const sc = cost.specialist;
    let line = `${esc(ho.concierge)} → ${esc(ho.specialist)} over A2A (${(ho.calls || []).length} call${(ho.calls || []).length === 1 ? "" : "s"})`;
    if (sp) line += `<br>specialist: ${sp.input_tokens} in / ${sp.output_tokens} out${sp.cached_tokens ? ` (${sp.cached_tokens} cached)` : ""}${sc ? ` · ${fmtUsd(sc.total_usd)}` : ""}${tm.specialist ? ` · ${(tm.specialist / 1000).toFixed(1)} s` : ""}${(sc && sc.agents && sc.agents[0] && sc.agents[0].tools && sc.agents[0].tools.length) ? ` · ${sc.agents[0].tools.map((x) => x.replace(/^mcp_[^.]*\./, "")).join(", ")}` : ""}`;
    else if (ho.usage_pending) line += `<br><span class="muted">specialist tokens: waiting for the Foundry trace (a few minutes)…</span> <a href="#" class="tc-reconcile" data-session="${esc(t.session_id || "")}">check now</a>`;
    else if (ho.reconcile_note) line += `<br><span class="muted">${esc(ho.reconcile_note)}</span>`;
    else if (cost.note) line += `<br><span class="muted">${esc(cost.note)}</span>`;
    html += `<div class="tc-row"><span class="k">handoff</span><span class="v">${line}</span></div>`;
  }
  if (tm.total != null) html += `<div class="tc-row"><span class="k">timeline</span><span class="v">${bar([{ label: "route", value: tm.route, cls: "c1", text: `${tm.route ?? "-"} ms` }, { label: "agent + retrieve", value: tm.agent, cls: "c2", text: `${tm.agent ?? "-"} ms` }, { label: "sources", value: tm.sources, cls: "c3", text: `${tm.sources ?? "-"} ms` }], tm.total)}</span></div>`;
  if (rt.calls) html += `<div class="tc-row"><span class="k">retrieval</span><span class="v"><span class="dp-tag ok">${rt.documents} chunks</span> <span class="dp-tag">${fmtK(rt.output_tokens)} tokens</span> <span class="dp-tag dim">${rt.calls} call${rt.calls > 1 ? "s" : ""}</span>${(rt.query_variants || []).length ? `<div class="qv">${rt.query_variants.map((x) => `<code>${esc(x)}</code>`).join("")}</div>` : ""}</span></div>`;
  if (tr.compliance && (tr.compliance.checked || tr.compliance.error)) {
    const c = tr.compliance; const bad = (c.violations || []).length; const rev = (c.review || []).length; const fix = (c.fixed || []).length;
    const tag = c.error ? `<span class="dp-tag warn">rules not applied: ${esc(c.error)}</span>` : bad ? `<span class="dp-tag warn">${bad} not compliant</span>` : `<span class="dp-tag ok">compliant</span>`;
    const detail = (c.findings || []).filter((f) => f.verdict === "non_compliant" || f.fixed)
      .map((f) => `${f.fixed ? "added" : "missing"}: ${f.clause} ${f.title}`).join("<br>");
    html += `<div class="tc-row"><span class="k">compliance</span><span class="v">${tag} <span class="dp-tag dim">${esc((c.product_names || []).join(", "))}</span>${fix ? ` <span class="dp-tag ok">${fix} warning added</span>` : ""}${rev ? ` <span class="dp-tag">${rev} to review</span>` : ""}${detail ? `<div class="sub">${esc(c.pack)} · ${detail}</div>` : ""}</span></div>`;
  }
  if (tr.reasoning && tr.reasoning.length) html += `<div class="tc-row"><span class="k">thinking</span><span class="v">${tr.reasoning.map(esc).join("<br>")}</span></div>`;
  if (u.agent || u.router) {
    const ai = (u.agent || {}).input_tokens || 0, ao = (u.agent || {}).output_tokens || 0, ri = (u.router || {}).input_tokens || 0, ro = (u.router || {}).output_tokens || 0;
    const sp = u.specialist || null;
    html += `<div class="tc-row"><span class="k">tokens</span><span class="v">${bar([{ label: tr.handoff ? "concierge in" : "agent in", value: ai, cls: "c2", text: fmtK(ai) }, ...(sp ? [{ label: "specialist in", value: sp.input_tokens, cls: "c3", text: fmtK(sp.input_tokens) }] : []), { label: "router in", value: ri, cls: "c1", text: fmtK(ri) }, { label: "out", value: ao + ro + (sp ? sp.output_tokens : 0), cls: "c4", text: fmtK(ao + ro + (sp ? sp.output_tokens : 0)) }])}${(u.total || {}).cached_tokens ? `<div class="sub">${fmtK(u.total.cached_tokens)} cached</div>` : ""}</span></div>`;
  }
  if (cost.agent || cost.router) html += `<div class="tc-row"><span class="k">cost</span><span class="v">${bar([{ label: tr.handoff ? "concierge" : "agent", value: (cost.agent || {}).total_usd, cls: "c2", text: fmtUsd((cost.agent || {}).total_usd) }, ...(cost.specialist ? [{ label: "specialist", value: cost.specialist.total_usd, cls: "c3", text: fmtUsd(cost.specialist.total_usd) }] : []), { label: "router", value: (cost.router || {}).total_usd, cls: "c1", text: fmtUsd((cost.router || {}).total_usd) }])}<div class="sub">${esc(cost.agent?.model || tr.model || "")}${cost.agent ? ` · in ${fmtUsd(cost.agent.input_usd)} · cached ${fmtUsd(cost.agent.cached_usd)} · out ${fmtUsd(cost.agent.output_usd)}` : ""}</div></span></div>`;
  for (const tc of t.tool_calls || []) if (tc.type === "mcp_call" && tc.error) html += `<div class="tc-row"><span class="k">error</span><span class="v"><span class="dp-tag warn">${esc(tc.error)}</span></span></div>`;
  if ((t.references || []).length) html += `<div class="tc-row"><span class="k">sources</span><span class="v"><ul class="dp-src">${t.references.map((r) => `<li><a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.title)}</a> <span class="sc">${esc(r.doc_type)} · ${r.score == null ? "-" : Number(r.score).toFixed(2)}</span></li>`).join("")}</ul></span></div>`;
  if ((t.citations || []).length) html += `<div class="tc-row"><span class="k">cited</span><span class="v">${t.citations.map((c) => `<a href="${esc(c.url)}" target="_blank" rel="noopener" class="lnk">${esc(c.title || c.url)}</a>`).join("<br>")}</span></div>`;
  if (t.error) html += `<div class="tc-row"><span class="k">error</span><span class="v"><span class="dp-tag warn">${esc(t.text)}</span></span></div>`;
  return html + "</div>";
}
  return { esc, fmtUsd, fmtK, bar, traceCard };
})();
