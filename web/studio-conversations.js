// Conversations tab: browse sessions, transcripts with trace cards, ratings and comments.
let cvSelected = null;
async function loadConversations() {
  const skill = $("#cv-skill").value, rating = $("#cv-rating").value;
  const list = await api(`/sessions/review?skill=${encodeURIComponent(skill)}&rating=${encodeURIComponent(rating)}`);
  const tb = $("#cv-table tbody"); tb.innerHTML = "";
  const skillsSeen = new Set();
  for (const s of list) {
    (s.skills || []).forEach((x) => skillsSeen.add(x));
    const tr = document.createElement("tr"); tr.className = "row" + (cvSelected === s.id ? " sel" : "");
    tr.innerHTML = `<td>${new Date(s.created_at).toLocaleString()}<br><span class="muted">${esc(s.source || "app")}</span></td><td>${esc(s.title || "(empty)")}</td><td>${s.turns}</td><td>${(s.skills || []).map((x) => `<span class="pill info">${esc(x)}</span>`).join(" ")}</td><td>${fmtUsd(s.cost_usd)}</td><td>${s.up || 0}/${s.down || 0}</td><td><button class="btn-danger del">✕</button></td>`;
    tr.addEventListener("click", (e) => { if (e.target.classList.contains("del")) return; cvSelected = s.id; openTranscript(s.id); });
    tr.querySelector(".del").addEventListener("click", async () => { if (!confirm("Delete this conversation?")) return; await api(`/sessions/${s.id}`, { method: "DELETE" }); loadConversations(); });
    tb.appendChild(tr);
  }
  const sel = $("#cv-skill"); const prev = sel.value; if (sel.options.length <= 1) { for (const x of Array.from(skillsSeen).sort()) { const o = document.createElement("option"); o.value = x; o.textContent = x; sel.appendChild(o); } sel.value = prev; }
  $("#cv-status").textContent = `${list.length} conversations`;
}
S.loaders.conversations = loadConversations;
$("#cv-refresh").addEventListener("click", loadConversations);
$("#cv-skill").addEventListener("change", loadConversations); $("#cv-rating").addEventListener("change", loadConversations);

async function openTranscript(id) {
  const rec = await api(`/sessions/${id}`); const fb = await api(`/feedback?session_id=${id}`).catch(() => []);
  const byIdx = Object.fromEntries(fb.map((f) => [f.idx, f]));
  let html = `<div class="toolbar"><b>${esc(rec.title)}</b><span class="muted">${rec.id} · ${rec.turns.length} messages · ${esc(rec.source || "app")}</span></div>`;
  rec.turns.forEach((t, i) => {
    if (t.role === "user") { html += `<div class="tr-user">${esc(t.text)}</div>`; return; }
    const f = byIdx[i] || {}; const q = rec.turns[i - 1]?.text || "";
    html += `<div class="tr-assistant"><div class="tr-text">${md(t.text)}</div><div class="tr-meta"><span class="pill info">${esc(t.skill_id)}</span> ${t.language ? `<span class="pill">${esc(t.language)}</span>` : ""} ${fmtUsd((t.trace || {}).cost?.total_usd)}
      <span class="rate" data-idx="${i}"><button class="${f.rating === "up" ? "on" : ""}" data-r="up">👍</button><button class="${f.rating === "down" ? "on" : ""}" data-r="down">👎</button></span>
      <input class="cmt" data-idx="${i}" placeholder="comment for the real-app team" value="${esc(f.comment || "")}"><button class="btn-secondary cmt-save" data-idx="${i}">save</button>${f.tester ? `<span class="muted">by ${esc(f.tester)}</span>` : ""}</div>
      <details><summary class="muted">trace</summary>${TR.traceCard({ ...t }, q)}</details></div>`;
  });
  $("#cv-transcript").innerHTML = html;
  $$("#cv-transcript .rate button").forEach((b) => b.addEventListener("click", async () => { const idx = +b.parentElement.dataset.idx; await api("/feedback", json({ session_id: id, idx, rating: b.dataset.r, comment: $(`#cv-transcript .cmt[data-idx='${idx}']`).value })); openTranscript(id); loadConversations(); }));
  $$("#cv-transcript .cmt-save").forEach((b) => b.addEventListener("click", async () => { const idx = +b.dataset.idx; const cur = byIdx[idx] || {}; await api("/feedback", json({ session_id: id, idx, rating: cur.rating || null, comment: $(`#cv-transcript .cmt[data-idx='${idx}']`).value })); openTranscript(id); }));
}
