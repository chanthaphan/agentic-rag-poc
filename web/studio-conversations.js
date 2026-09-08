// Conversations tab: flat question list with search/filters and checkboxes, session list, transcript with ratings,
// and a selection box that exports the chosen questions to xlsx or appends them to an eval set.
bindSubtabs("data-cpane", "cpane-");
const CV = { rows: [], page: 0, size: 20, box: new Map(), session: null };
const boxKey = (r) => `${r.session_id}:${r.idx}`;
try { for (const r of JSON.parse(localStorage.getItem("cv-box") || "[]")) CV.box.set(boxKey(r), r); } catch {}
function saveBox() { try { localStorage.setItem("cv-box", JSON.stringify(Array.from(CV.box.values()))); } catch {} renderBox(); }

async function loadConversations() {
  const params = new URLSearchParams({ skill: $("#cv-skill").value, rating: $("#cv-rating").value, q: $("#cv-q").value.trim(), source: $("#cv-source").value, user: $("#cv-user").value, limit: 500 });
  $("#cv-status").textContent = "loading…";
  const [rows, list, users] = await Promise.all([api(`/conversations/questions?${params}`), api(`/sessions/review?skill=${encodeURIComponent($("#cv-skill").value)}&rating=${encodeURIComponent($("#cv-rating").value === "any" ? "" : $("#cv-rating").value)}&user=${encodeURIComponent($("#cv-user").value)}`), api("/conversations/users").catch(() => [])]);
  const us = $("#cv-user"); if (us.options.length <= 1) for (const u of users) { const o = document.createElement("option"); o.value = u; o.textContent = u; us.appendChild(o); }
  CV.rows = rows; CV.page = 0; renderCvQuestions();
  const sel = $("#cv-skill"); if (sel.options.length <= 1) { const skills = new Set(rows.map((r) => r.skill_id).filter(Boolean)); list.forEach((s) => (s.skills || []).forEach((x) => skills.add(x))); for (const x of Array.from(skills).sort()) { const o = document.createElement("option"); o.value = x; o.textContent = x; sel.appendChild(o); } }
  const tb = $("#cv-table tbody"); tb.innerHTML = "";
  for (const s of list) {
    const tr = document.createElement("tr"); tr.className = "row" + (CV.session === s.id ? " sel" : "");
    tr.innerHTML = `<td>${new Date(s.created_at).toLocaleString()}<br><span class="muted">${esc(s.source || "app")}</span></td><td>${s.user_name ? `<b>${esc(s.user_name)}</b><br><span class="muted">${esc(s.user_email || "")}</span>` : '<span class="muted">unknown</span>'}</td><td>${esc(s.title || "(empty)")}</td><td>${s.turns}</td><td>${(s.skills || []).map((x) => `<span class="pill info">${esc(x)}</span>`).join(" ")}</td><td>${fmtUsd(s.cost_usd)}</td><td>${s.up || 0}/${s.down || 0}</td><td><button class="btn-danger del">✕</button></td>`;
    tr.addEventListener("click", (e) => { if (e.target.classList.contains("del")) return; openTranscript(s.id); });
    tr.querySelector(".del").addEventListener("click", async () => { if (!confirm("Delete this conversation?")) return; await api(`/sessions/${s.id}`, { method: "DELETE" }); loadConversations(); });
    tb.appendChild(tr);
  }
  $("#cv-status").textContent = `${rows.length} questions in ${list.length} conversations`;
  renderBox();
}
S.loaders.conversations = loadConversations;
["#cv-refresh"].forEach((s) => $(s).addEventListener("click", loadConversations));
["#cv-skill", "#cv-rating", "#cv-source", "#cv-user"].forEach((s) => $(s).addEventListener("change", loadConversations));
let cvTimer; $("#cv-q").addEventListener("input", () => { clearTimeout(cvTimer); cvTimer = setTimeout(loadConversations, 350); });

function renderCvQuestions() {
  const pages = Math.max(1, Math.ceil(CV.rows.length / CV.size)); CV.page = Math.min(CV.page, pages - 1);
  const start = CV.page * CV.size; const slice = CV.rows.slice(start, start + CV.size);
  const tb = $("#cv-qtable tbody"); tb.innerHTML = "";
  for (const r of slice) {
    const k = boxKey(r); const tr = document.createElement("tr"); tr.className = CV.box.has(k) ? "sel" : ""; tr.dataset.key = k;
    tr.innerHTML = `<td><input type="checkbox" class="pick" ${CV.box.has(k) ? "checked" : ""}></td><td><span class="qtext" title="open the conversation">${esc(r.question)}</span><span class="ans">${esc((r.answer || "").slice(0, 140))}${(r.answer || "").length > 140 ? "…" : ""}</span></td><td><span class="pill info">${esc(r.skill_id || "?")}</span>${r.language ? ` <span class="pill">${esc(r.language)}</span>` : ""}</td><td>${r.rating === "up" ? "👍" : r.rating === "down" ? "👎" : ""}${r.comment ? ` <span class="muted" title="${esc(r.comment)}">💬</span>` : ""}</td><td>${fmtUsd(r.cost_usd)}</td><td>${r.user ? `<b>${esc(r.user)}</b><br>` : ""}<span class="muted">${r.at ? new Date(r.at).toLocaleString() : ""} · ${esc(r.source)}</span></td><td><button class="btn-secondary open">open</button></td>`;
    tr.querySelector(".pick").addEventListener("change", (e) => { toggle(r, e.target.checked); tr.classList.toggle("sel", e.target.checked); });
    tr.querySelector(".qtext").addEventListener("click", () => openTranscript(r.session_id, r.idx));
    tr.querySelector(".open").addEventListener("click", () => openTranscript(r.session_id, r.idx));
    tb.appendChild(tr);
  }
  if (!slice.length) tb.innerHTML = `<tr><td colspan="7" class="muted">no questions match</td></tr>`;
  $("#cv-shown").textContent = `${CV.rows.length ? start + 1 : 0}–${Math.min(start + CV.size, CV.rows.length)} of ${CV.rows.length}`;
  $("#cv-check-all").checked = slice.length > 0 && slice.every((r) => CV.box.has(boxKey(r)));
  const pager = $("#cv-pager"); pager.hidden = CV.rows.length <= CV.size;
  $("#cv-page-info").textContent = `page ${CV.page + 1} / ${pages}`; $("#cv-prev").disabled = CV.page === 0; $("#cv-next").disabled = CV.page >= pages - 1;
}
$("#cv-prev").addEventListener("click", () => { CV.page--; renderCvQuestions(); });
$("#cv-next").addEventListener("click", () => { CV.page++; renderCvQuestions(); });
$("#cv-check-all").addEventListener("change", (e) => { const start = CV.page * CV.size; for (const r of CV.rows.slice(start, start + CV.size)) toggle(r, e.target.checked, true); saveBox(); renderCvQuestions(); });

function toggle(r, on, silent = false) {
  const k = boxKey(r);
  if (on) CV.box.set(k, { session_id: r.session_id, idx: r.idx, question: r.question, skill_id: r.skill_id || "", language: r.language || "" }); else CV.box.delete(k);
  if (!silent) saveBox();
}

// ---- selection box ----
function renderBox() {
  const items = Array.from(CV.box.values()); $("#cv-box-count").textContent = items.length;
  $("#cv-box-list").innerHTML = items.map((r) => `<div class="bi" data-key="${esc(boxKey(r))}"><span><span class="q" title="${esc(r.question)}">${esc(r.question)}</span><span class="s">${esc(r.skill_id || "?")}${r.language ? " · " + esc(r.language) : ""}</span></span><button class="btn-secondary rm" title="remove">✕</button></div>`).join("") || `<div class="muted">empty</div>`;
  $("#cv-box-list").querySelectorAll(".rm").forEach((b) => b.addEventListener("click", () => { CV.box.delete(b.parentElement.dataset.key); saveBox(); renderCvQuestions(); }));
  ["#cv-box-xlsx", "#cv-to-routing", "#cv-to-rag", "#cv-to-quality"].forEach((s) => { $(s).disabled = !items.length; });
}
$("#cv-box-clear").addEventListener("click", () => { CV.box.clear(); saveBox(); renderCvQuestions(); });
$("#cv-box-xlsx").addEventListener("click", async () => {
  const items = Array.from(CV.box.values()).map((r) => ({ session_id: r.session_id, idx: r.idx }));
  $("#cv-box-status").textContent = "building workbook…";
  try {
    const res = await fetch(new URL("/conversations/export.xlsx", location.origin), { credentials: "same-origin", ...json({ items }) });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob(); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "bankrag-questions.xlsx"; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    $("#cv-box-status").textContent = `exported ${items.length} question(s)`;
  } catch (e) { $("#cv-box-status").textContent = e.message; }
});
async function sendToEval(set) {
  const items = Array.from(CV.box.values());
  const cases = items.map((r) => set === "routing" ? { q: r.question, skill: r.skill_id || "" } : set === "rag" ? { q: r.question, skill: r.skill_id || null, expect: [], require_source: true } : { q: r.question, skill: r.skill_id || null });
  $("#cv-box-status").textContent = "adding…";
  try { const r = await api(`/evals/${set}/append`, json({ cases })); $("#cv-box-status").textContent = `${set}: added ${r.added}, skipped ${r.skipped} duplicate(s), ${r.total} in the set now. Open the Evals tab to review${set === "rag" ? " and fill expected substrings" : set === "quality" ? " and optionally add expected answers" : ""}.`; } catch (e) { $("#cv-box-status").textContent = e.message; }
}
$("#cv-to-routing").addEventListener("click", () => sendToEval("routing"));
$("#cv-to-rag").addEventListener("click", () => sendToEval("rag"));
$("#cv-to-quality").addEventListener("click", () => sendToEval("quality"));

// ---- transcript ----
async function openTranscript(id, focusIdx = null) {
  const rec = await api(`/sessions/${id}`); const fb = await api(`/feedback?session_id=${id}`).catch(() => []);
  const byIdx = Object.fromEntries(fb.map((f) => [f.idx, f]));
  CV.session = id; $("#cv-transcript-panel").hidden = false;
  $("#cv-transcript-title").textContent = rec.title || "(untitled)"; $("#cv-transcript-sub").textContent = `${rec.user_name ? `${rec.user_name}${rec.user_email ? ` <${rec.user_email}>` : ""} · ` : ""}${rec.id} · ${rec.turns.length} messages · ${rec.source || "app"} · ${new Date(rec.created_at).toLocaleString()}`;
  let html = "";
  rec.turns.forEach((t, i) => {
    if (t.role === "user") {
      const a = rec.turns[i + 1]; const r = { session_id: id, idx: i, question: t.text, skill_id: a?.skill_id || "", language: a?.language || "" }; const k = boxKey(r);
      html += `<div class="tr-row"><label><input type="checkbox" class="tpick" data-idx="${i}" ${CV.box.has(k) ? "checked" : ""}> select</label><div class="tr-user" id="tr-${i}">${t.by || rec.user_name ? `<span class="tr-by">${esc(t.by || rec.user_name)}</span>` : ""}${esc(t.text)}</div></div>`; return;
    }
    const f = byIdx[i] || {}; const q = rec.turns[i - 1]?.text || "";
    html += `<div class="tr-assistant"><div class="tr-text">${md(t.text)}</div><div class="tr-meta"><span class="pill info">${esc(t.skill_id)}</span> ${t.language ? `<span class="pill">${esc(t.language)}</span>` : ""} ${fmtUsd((t.trace || {}).cost?.total_usd)}
      <span class="rate" data-idx="${i}"><button class="${f.rating === "up" ? "on" : ""}" data-r="up">👍</button><button class="${f.rating === "down" ? "on" : ""}" data-r="down">👎</button></span>
      <input class="cmt" data-idx="${i}" placeholder="comment for the real-app team" value="${esc(f.comment || "")}"><button class="btn-secondary cmt-save" data-idx="${i}">save</button>${f.tester ? `<span class="muted">by ${esc(f.tester)}</span>` : ""}</div>
      <details><summary class="muted">trace</summary>${TR.traceCard({ ...t }, q)}</details></div>`;
  });
  $("#cv-transcript").innerHTML = html;
  $$("#cv-transcript .tpick").forEach((cb) => cb.addEventListener("change", () => { const i = +cb.dataset.idx; const a = rec.turns[i + 1]; toggle({ session_id: id, idx: i, question: rec.turns[i].text, skill_id: a?.skill_id || "", language: a?.language || "" }, cb.checked); renderCvQuestions(); }));
  $$("#cv-transcript .rate button").forEach((b) => b.addEventListener("click", async () => { const idx = +b.parentElement.dataset.idx; await api("/feedback", json({ session_id: id, idx, rating: b.dataset.r, comment: $(`#cv-transcript .cmt[data-idx='${idx}']`).value })); openTranscript(id); loadConversations(); }));
  $$("#cv-transcript .cmt-save").forEach((b) => b.addEventListener("click", async () => { const idx = +b.dataset.idx; const cur = byIdx[idx] || {}; await api("/feedback", json({ session_id: id, idx, rating: cur.rating || null, comment: $(`#cv-transcript .cmt[data-idx='${idx}']`).value })); openTranscript(id); }));
  $("#cv-select-session").onclick = () => { rec.turns.forEach((t, i) => { if (t.role === "user") { const a = rec.turns[i + 1]; toggle({ session_id: id, idx: i, question: t.text, skill_id: a?.skill_id || "", language: a?.language || "" }, true, true); } }); saveBox(); openTranscript(id); renderCvQuestions(); };
  $$("#cv-table tr.row").forEach((tr) => tr.classList.remove("sel"));
  $("#cv-transcript-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  if (focusIdx != null) { const el = $(`#tr-${focusIdx}`); if (el) { el.style.outline = "2px solid #ffd166"; setTimeout(() => (el.style.outline = ""), 2500); el.scrollIntoView({ behavior: "smooth", block: "center" }); } }
}
$("#cv-transcript-close").addEventListener("click", () => { $("#cv-transcript-panel").hidden = true; CV.session = null; });
