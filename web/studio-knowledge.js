// Knowledge tab: categories, add content (upload / crawl / import), files with PDF status, ingestion panel with
// structured progress (phase stepper, bar, counters, log, recent runs), index search, chunk browser.
function category() { return $("#kn-newcat").value.trim() || $("#kn-category").value; }
bindSubtabs("data-kpane", "kpane-");

const IG = { job: null, timer: null, lastList: 0 };
const PHASES = {
  ingest: [["scan", "Scan"], ["embed", "Embed"], ["upload", "Upload"], ["done", "Done"]],
  reingest: [["scan", "Scan"], ["embed", "Embed"], ["upload", "Upload"], ["done", "Done"]],
  crawl: [["crawl", "Crawl"], ["pdfs", "PDFs"], ["scan", "Scan"], ["embed", "Embed"], ["upload", "Upload"], ["done", "Done"]],
  import: [["import", "Fetch"], ["scan", "Scan"], ["embed", "Embed"], ["upload", "Upload"], ["done", "Done"]],
};
const PHASE_UNIT = { scan: "files", embed: "chunks", upload: "chunks", crawl: "pages", pdfs: "PDFs", import: "URLs" };
const STAT_LABEL = { added: "added", updated: "updated", unchanged: "unchanged", skipped: "skipped", deleted: "removed", chunks: "chunks", uploaded: "uploaded", deleted_chunks: "chunks dropped", pages: "pages", pdfs: "PDFs", errors: "errors", queued: "queued", imported: "imported" };
const fmtMs = (ms) => ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;

async function loadKnowledge() {
  const d = await api("/knowledge/stats"); const sel = $("#kn-category"); const prev = sel.value; sel.innerHTML = "";
  for (const r of d.rows) { const o = document.createElement("option"); o.value = r.category; o.textContent = `${r.category} (${r.files} files, ${r.indexed_chunks} chunks)`; sel.appendChild(o); }
  if (prev) sel.value = prev;
  $("#kn-cards").innerHTML = d.rows.map((r) => `<div class="card"><div class="v">${r.indexed_chunks}</div><div class="k">${esc(r.category)} · ${r.files} files</div></div>`).join("") +
    `<div class="card"><div class="v">${d.index.document_count ?? "?"}</div><div class="k">index docs · ${((d.index.storage_size || 0) / 1e6).toFixed(1)} MB + ${((d.index.vector_index_size || 0) / 1e6).toFixed(1)} MB vectors</div></div>` + (d.error ? `<div class="card"><div class="k">${esc(d.error)}</div></div>` : "");
  const ks = $("#kn-skill"); if (!ks.options.length) { const skills = await api("/skills?remote=false"); for (const s of skills) { const o = document.createElement("option"); o.value = s.id; o.textContent = s.id; ks.appendChild(o); } }
  await loadFiles();
  loadIngestRuns();
}
S.loaders.knowledge = loadKnowledge;

const FILES = { all: [], page: 0, size: 15 };
async function loadFiles() {
  const cat = category();
  $$(".kn-cat-name").forEach((el) => el.textContent = cat || "…");
  try { FILES.all = await api(`/knowledge/files?category=${encodeURIComponent(cat)}`); } catch (e) { FILES.all = []; $("#kn-status").textContent = e.message; }
  FILES.page = 0;
  renderFiles();
  const pending = FILES.all.filter((f) => !f.indexed).length;
  $("#kn-status").textContent = "";
  if (!IG.job || IG.job.status !== "running") $("#ig-hint").textContent = pending ? `${pending} file(s) in ${cat} are waiting to be ingested.` : FILES.all.length ? `Everything in ${cat} is indexed. Run ingest after adding or editing files; full re-ingest re-embeds every file.` : "";
}

function renderFiles() {
  const q = $("#kn-filter").value.trim().toLowerCase();
  const files = q ? FILES.all.filter((f) => (f.path + " " + (f.title || "")).toLowerCase().includes(q)) : FILES.all;
  const pages = Math.max(1, Math.ceil(files.length / FILES.size)); FILES.page = Math.min(FILES.page, pages - 1);
  const start = FILES.page * FILES.size; const slice = files.slice(start, start + FILES.size);
  const tb = $("#kn-files-table tbody"); tb.innerHTML = "";
  for (const f of slice) {
    const kind = f.kind || (f.path.endsWith(".pdf") ? "pdf" : "md");
    const status = kind === "pdf" ? (f.image_based ? '<span class="pill warn">image-only PDF (no text)</span>' : f.text_chars != null ? `<span class="pill ok">text ${(f.text_chars / 1000).toFixed(1)}k chars</span>` : "") : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><a href="#" class="chunks" data-path="${esc(f.path)}">${esc(f.path)}</a><br><span class="muted">${esc(f.title || "")}</span></td><td>${esc(kind)} ${status}</td><td>${(f.size / 1024).toFixed(1)} KB</td><td>${f.chunks}${f.indexed ? "" : ' <span class="pill info">not ingested</span>'}</td><td><button class="btn-secondary reingest" data-path="${esc(f.path)}">re-ingest</button> <button class="btn-danger del" data-path="${esc(f.path)}">delete</button></td>`;
    tb.appendChild(tr);
  }
  if (!slice.length) tb.innerHTML = `<tr><td colspan="5" class="muted">${FILES.all.length ? "no file matches the filter" : "no files yet — upload, crawl or import above"}</td></tr>`;
  tb.querySelectorAll(".del").forEach((b) => b.addEventListener("click", async () => { if (!confirm(`Delete ${b.dataset.path}? Run ingest afterwards to drop its chunks.`)) return; await api(`/knowledge/files?path=${encodeURIComponent(b.dataset.path)}`, { method: "DELETE" }); loadFiles(); }));
  tb.querySelectorAll(".reingest").forEach((b) => b.addEventListener("click", async () => { const j = await api(`/knowledge/reingest?path=${encodeURIComponent(b.dataset.path)}`, { method: "POST" }); watchJob(j.job_id); }));
  tb.querySelectorAll(".chunks").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showChunks(a.dataset.path); }));
  const pending = FILES.all.filter((f) => !f.indexed).length, imageOnly = FILES.all.filter((f) => f.image_based).length;
  $("#kn-files-hint").textContent = `${FILES.all.length} file(s)` + (pending ? ` · ${pending} not ingested` : "") + (imageOnly ? ` · ${imageOnly} image-only PDF` : "");
  const pager = $("#kn-pager"); pager.hidden = files.length <= FILES.size;
  $("#kn-page-info").textContent = `${files.length ? start + 1 : 0}–${Math.min(start + FILES.size, files.length)} of ${files.length}`;
  $("#kn-prev").disabled = FILES.page === 0; $("#kn-next").disabled = FILES.page >= pages - 1;
}
$("#kn-filter").addEventListener("input", () => { FILES.page = 0; renderFiles(); });
$("#kn-prev").addEventListener("click", () => { FILES.page--; renderFiles(); });
$("#kn-next").addEventListener("click", () => { FILES.page++; renderFiles(); });
$("#kn-category").addEventListener("change", () => { $("#kn-newcat").value = ""; loadFiles(); });
$("#kn-newcat").addEventListener("input", () => { $$(".kn-cat-name").forEach((el) => el.textContent = category() || "…"); });
$("#kn-refresh").addEventListener("click", loadKnowledge);

// ---- add content ----
async function uploadFiles(fileList) {
  const fd = new FormData(); for (const f of fileList) fd.append("files", f);
  try { const r = await api(`/knowledge/upload?category=${encodeURIComponent(category())}`, { method: "POST", body: fd }); $("#kn-status").textContent = `saved ${r.saved.length} file(s) into ${category()}`; await loadKnowledge(); } catch (err) { $("#kn-status").textContent = err.message; }
}
$("#kn-files").addEventListener("change", async (e) => { await uploadFiles(e.target.files); e.target.value = ""; });
const dz = $("#kn-drop");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("on"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("on"); }));
dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });

async function startJob(fn) {
  if (!category()) { $("#kn-status").textContent = "pick a category first"; return; }
  if (IG.job && IG.job.status === "running") { $("#kn-status").textContent = "a job is already running — wait for it to finish"; return; }
  try { const j = await fn(); watchJob(j.job_id); } catch (e) { $("#kn-status").textContent = e.message; }
}
$("#kn-ingest").addEventListener("click", () => startJob(() => api(`/knowledge/ingest?category=${encodeURIComponent(category())}&full=false`, { method: "POST" })));
$("#kn-ingest-full").addEventListener("click", () => { if (confirm(`Re-embed every file in ${category()}? This costs embedding tokens for all chunks.`)) startJob(() => api(`/knowledge/ingest?category=${encodeURIComponent(category())}&full=true`, { method: "POST" })); });
$("#kn-import").addEventListener("click", () => {
  const urls = $("#kn-urls").value.split("\n").map((x) => x.trim()).filter(Boolean); if (!urls.length) { $("#kn-status").textContent = "enter at least one URL"; return; }
  startJob(() => api("/knowledge/import-url", json({ category: category(), urls, ingest: $("#kn-import-ingest").checked })));
});
$("#cr-run").addEventListener("click", () => {
  const start_urls = $("#cr-start").value.split("\n").map((x) => x.trim()).filter(Boolean); if (!start_urls.length) { $("#kn-status").textContent = "enter a start URL"; return; }
  const include_prefixes = $("#cr-prefix").value.split("\n").map((x) => x.trim()).filter(Boolean);
  startJob(() => api("/knowledge/crawl", json({ category: category(), start_urls, include_prefixes, max_pages: +$("#cr-max").value || 30, include_pdfs: $("#cr-pdf").checked, ingest: $("#cr-ingest").checked })));
});

// ---- ingestion panel ----
function watchJob(id) {
  clearInterval(IG.timer);
  $("#ig-body").hidden = false; $("#ig-logwrap").open = false;
  const tick = async () => {
    let j; try { j = await api(`/jobs/${id}`); } catch (e) { clearInterval(IG.timer); $("#ig-msg").textContent = e.message; return; }
    IG.job = j; renderJob(j);
    if (j.status !== "running") { clearInterval(IG.timer); IG.timer = null; await loadKnowledge(); }
  };
  tick(); IG.timer = setInterval(tick, 700);
}

function renderJob(j) {
  const pr = j.progress || {}; const phases = PHASES[j.kind] || PHASES.ingest;
  const running = j.status === "running", failed = j.status === "error";
  const state = $("#ig-state"); state.textContent = running ? `${j.kind} running` : failed ? `${j.kind} failed` : `${j.kind} done`; state.className = "pill " + (running ? "run" : failed ? "err" : "ok");
  $("#ig-hint").textContent = `${j.kind} · ${esc(j.meta?.category || "")}${j.meta?.full ? " · full re-ingest" : ""}${j.meta?.path ? " · " + j.meta.path : ""}${j.meta?.start ? " · " + j.meta.start : ""}`;
  // stepper
  let idx = phases.findIndex(([k]) => k === pr.phase); if (pr.phase === "error") idx = Math.max(0, phases.findIndex(([k]) => k === (IG.lastPhase || "scan")));
  if (pr.phase && pr.phase !== "error") IG.lastPhase = pr.phase;
  $("#ig-steps").innerHTML = phases.map(([k, label], i) => `<li class="${failed && i === idx ? "err" : j.status === "done" || i < idx ? "done" : i === idx ? "on" : ""}">${label}</li>`).join("");
  // bar
  const fill = $("#ig-bar"); const has = pr.total > 0 && pr.done != null; const pct = j.status === "done" ? 100 : has ? Math.round(100 * pr.done / pr.total) : null;
  fill.className = "fill" + (failed ? " err" : j.status === "done" ? " ok" : pct == null && running ? " indet" : ""); fill.style.width = pct == null ? "" : pct + "%";
  $("#ig-pct").textContent = failed ? "failed" : j.status === "done" ? "100%" : pct == null ? "…" : pct + "%";
  $("#ig-count").textContent = has && running ? `${pr.done} / ${pr.total} ${PHASE_UNIT[pr.phase] || ""}` : j.status === "done" ? doneSummary(j) : "";
  $("#ig-elapsed").textContent = fmtMs(j.elapsed_ms || 0);
  $("#ig-msg").textContent = failed ? pr.message : running ? (pr.message || "") : "";
  $("#ig-msg").classList.toggle("err", failed);
  // counters
  const st = pr.stats || {};
  $("#ig-stats").innerHTML = Object.entries(STAT_LABEL).filter(([k]) => st[k] != null && (st[k] !== 0 || ["added", "updated", "unchanged"].includes(k) && j.kind !== "crawl")).map(([k, label]) => `<span class="igchip ${k}"><b>${st[k]}</b>${label}</span>`).join("");
  // log
  const log = $("#kn-log"); log.textContent = (j.log || []).join("\n"); if (!IG.pinned) log.scrollTop = 1e9;
  if (failed) $("#ig-logwrap").open = true;
}

function doneSummary(j) {
  const r = j.result || {}; const s = r.summary || r.ingest || {}; const parts = [];
  if (r.crawl) parts.push(`${r.crawl.pages} pages, ${r.crawl.pdfs} PDFs${r.crawl.errors ? `, ${r.crawl.errors} errors` : ""}`);
  if (r.saved) parts.push(`${r.saved.length} imported`);
  for (const k of ["added", "updated", "unchanged", "skipped", "deleted"]) if (s[k]) parts.push(`${s[k]} ${k}`);
  if (r.uploaded) parts.push(`${r.uploaded} chunks up`);
  return parts.join(" · ") || "finished";
}

async function loadIngestRuns() {
  let runs = []; try { runs = await api("/jobs?kind=ingest,reingest,crawl,import&limit=8"); } catch { return; }
  const box = $("#ig-runs");
  if (!runs.length) { box.textContent = "none yet"; return; }
  box.innerHTML = runs.map((j) => `<div class="run" data-id="${j.id}"><span class="pill ${j.status === "running" ? "run" : j.status === "error" ? "err" : "ok"}">${j.status}</span><span><span class="k">${esc(j.kind)}</span> ${esc(j.meta?.category || "")}<br><span class="s">${esc(j.status === "error" ? (j.progress?.message || "failed") : j.status === "running" ? `${j.progress?.phase || ""} ${j.progress?.total ? `${j.progress.done}/${j.progress.total}` : ""}` : doneSummary(j))}</span></span><span class="s">${fmtMs(j.elapsed_ms || 0)}<br>${new Date(j.started).toLocaleTimeString()}</span></div>`).join("");
  box.querySelectorAll(".run").forEach((el) => el.addEventListener("click", () => watchJob(el.dataset.id)));
  const live = runs.find((j) => j.status === "running"); if (live && !IG.timer) watchJob(live.id);
}

// ---- search + chunk browser ----
$("#kn-retrieve").addEventListener("click", async () => {
  const q = $("#kn-q").value.trim(); if (!q) return; $("#kn-refs").innerHTML = "<span class='muted'>searching…</span>";
  try {
    const [refs, hits] = await Promise.all([
      api(`/knowledge/retrieve?q=${encodeURIComponent(q)}&skill=${encodeURIComponent($("#kn-skill").value)}`).catch((e) => ({ error: e.message })),
      api(`/knowledge/search?q=${encodeURIComponent(q)}&category=${encodeURIComponent(category())}&k=5`).catch((e) => ({ error: e.message })),
    ]);
    const block = (title, items, mapper) => `<h4>${title}</h4>` + (items.error ? `<span class="err">${esc(items.error)}</span>` : items.length ? items.map(mapper).join("") : "<span class='muted'>no results</span>");
    $("#kn-refs").innerHTML =
      block(`Knowledge base retrieve (skill ${esc($("#kn-skill").value)})`, refs, (r) => `<div class="ref"><div class="t">${esc(r.title)}</div><div class="s">${esc(r.doc_type)} · ${esc(r.product_name)} · score ${r.score ?? "-"}</div><a href="${esc(r.source_url)}" target="_blank">${esc(r.source_url)}</a><div>${esc(r.snippet)}</div></div>`) +
      block(`Index hybrid search (category ${esc(category())})`, hits, (h) => `<div class="ref"><div class="t">${esc(h.title)} <span class="muted">#${h.chunk_index}</span></div><div class="s">${esc(h.breadcrumb)} · score ${(h.score || 0).toFixed(3)}${h.reranker_score != null ? ` · reranker ${h.reranker_score.toFixed(2)}` : ""}</div><div>${esc(h.snippet)}</div></div>`);
  } catch (err) { $("#kn-refs").innerHTML = `<span class="err">${esc(err.message)}</span>`; }
});
$("#kn-q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#kn-retrieve").click(); });

async function showChunks(path) {
  $("#kn-chunks").hidden = false; $("#kn-chunks-title").textContent = path; $("#kn-chunks-body").innerHTML = "<span class='muted'>loading…</span>";
  try { const chunks = await api(`/knowledge/chunks?path=${encodeURIComponent(path)}`);
    $("#kn-chunks-body").innerHTML = chunks.length ? chunks.map((c) => `<div class="chunk"><div class="s"><b>#${c.chunk_index}</b> · ${esc(c.breadcrumb)} · ${c.tokens} tokens</div><div class="c">${esc(c.content)}</div></div>`).join("") : "<span class='muted'>no chunks indexed for this file (run ingest)</span>";
    $("#kn-chunks").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { $("#kn-chunks-body").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
$("#kn-chunks-close").addEventListener("click", () => { $("#kn-chunks").hidden = true; });
$("#kn-log").addEventListener("scroll", () => { const l = $("#kn-log"); IG.pinned = l.scrollHeight - l.scrollTop - l.clientHeight > 30; });
$("#ig-logwrap").addEventListener("toggle", () => { IG.pinned = false; $("#kn-log").scrollTop = 1e9; });
