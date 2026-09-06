// Knowledge tab: categories, files (with PDF status), upload/drag-drop, ingest, URL import, index search, chunk browser.
function category() { return $("#kn-newcat").value.trim() || $("#kn-category").value; }

async function loadKnowledge() {
  const d = await api("/knowledge/stats"); const sel = $("#kn-category"); const prev = sel.value; sel.innerHTML = "";
  for (const r of d.rows) { const o = document.createElement("option"); o.value = r.category; o.textContent = `${r.category} (${r.files} files, ${r.indexed_chunks} chunks)`; sel.appendChild(o); }
  if (prev) sel.value = prev;
  $("#kn-cards").innerHTML = d.rows.map((r) => `<div class="card"><div class="v">${r.indexed_chunks}</div><div class="k">${esc(r.category)} · ${r.files} files</div></div>`).join("") +
    `<div class="card"><div class="v">${d.index.document_count ?? "?"}</div><div class="k">index docs · ${((d.index.storage_size || 0) / 1e6).toFixed(1)} MB + ${((d.index.vector_index_size || 0) / 1e6).toFixed(1)} MB vectors</div></div>` + (d.error ? `<div class="card"><div class="k">${esc(d.error)}</div></div>` : "");
  const ks = $("#kn-skill"); if (!ks.options.length) { const skills = await api("/skills?remote=false"); for (const s of skills) { const o = document.createElement("option"); o.value = s.id; o.textContent = s.id; ks.appendChild(o); } }
  await loadFiles();
}
S.loaders.knowledge = loadKnowledge;

async function loadFiles() {
  const cat = category(); const tb = $("#kn-files-table tbody"); tb.innerHTML = "";
  let files = []; try { files = await api(`/knowledge/files?category=${encodeURIComponent(cat)}`); } catch (e) { $("#kn-status").textContent = e.message; }
  for (const f of files) {
    const kind = f.kind || (f.path.endsWith(".pdf") ? "pdf" : "md");
    const status = kind === "pdf" ? (f.image_based ? '<span class="pill warn">image-only PDF (no text)</span>' : f.text_chars != null ? `<span class="pill ok">text ${(f.text_chars / 1000).toFixed(1)}k chars</span>` : "") : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><a href="#" class="chunks" data-path="${esc(f.path)}">${esc(f.path)}</a><br><span class="muted">${esc(f.title || "")}</span></td><td>${esc(kind)} ${status}</td><td>${(f.size / 1024).toFixed(1)} KB</td><td>${f.chunks}${f.indexed ? "" : ' <span class="pill info">not ingested</span>'}</td><td><button class="btn-secondary reingest" data-path="${esc(f.path)}">re-ingest</button> <button class="btn-danger del" data-path="${esc(f.path)}">delete</button></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll(".del").forEach((b) => b.addEventListener("click", async () => { if (!confirm(`Delete ${b.dataset.path}? Run ingest afterwards to drop its chunks.`)) return; await api(`/knowledge/files?path=${encodeURIComponent(b.dataset.path)}`, { method: "DELETE" }); loadFiles(); }));
  tb.querySelectorAll(".reingest").forEach((b) => b.addEventListener("click", async () => { const j = await api(`/knowledge/reingest?path=${encodeURIComponent(b.dataset.path)}`, { method: "POST" }); pollJob(j.job_id, $("#kn-log"), $("#kn-status"), loadKnowledge); }));
  tb.querySelectorAll(".chunks").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); showChunks(a.dataset.path); }));
  $("#kn-status").textContent = `${files.length} files in ${cat}`;
}
$("#kn-category").addEventListener("change", () => { $("#kn-newcat").value = ""; loadFiles(); });
$("#kn-refresh").addEventListener("click", loadKnowledge);

async function uploadFiles(fileList) {
  const fd = new FormData(); for (const f of fileList) fd.append("files", f);
  try { const r = await api(`/knowledge/upload?category=${encodeURIComponent(category())}`, { method: "POST", body: fd }); $("#kn-status").textContent = `saved ${r.saved.length} file(s); run ingest`; await loadKnowledge(); } catch (err) { $("#kn-status").textContent = err.message; }
}
$("#kn-files").addEventListener("change", async (e) => { await uploadFiles(e.target.files); e.target.value = ""; });
const dz = $("#kn-drop");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("on"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("on"); }));
dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });

async function runIngest(full) { const j = await api(`/knowledge/ingest?category=${encodeURIComponent(category())}&full=${full}`, { method: "POST" }); pollJob(j.job_id, $("#kn-log"), $("#kn-status"), loadKnowledge); }
$("#kn-ingest").addEventListener("click", () => runIngest(false));
$("#kn-ingest-full").addEventListener("click", () => runIngest(true));

$("#kn-import").addEventListener("click", async () => {
  const urls = $("#kn-urls").value.split("\n").map((x) => x.trim()).filter(Boolean); if (!urls.length) return;
  try { const j = await api("/knowledge/import-url", json({ category: category(), urls, ingest: $("#kn-import-ingest").checked })); pollJob(j.job_id, $("#kn-log"), $("#kn-status"), loadKnowledge); } catch (e) { $("#kn-status").textContent = e.message; }
});

$("#cr-run").addEventListener("click", async () => {
  const start_urls = $("#cr-start").value.split("\n").map((x) => x.trim()).filter(Boolean); if (!start_urls.length) { $("#kn-status").textContent = "enter a start URL"; return; }
  const include_prefixes = $("#cr-prefix").value.split("\n").map((x) => x.trim()).filter(Boolean);
  try { const j = await api("/knowledge/crawl", json({ category: category(), start_urls, include_prefixes, max_pages: +$("#cr-max").value || 30, include_pdfs: $("#cr-pdf").checked, ingest: $("#cr-ingest").checked })); $("#kn-status").textContent = "crawling…"; pollJob(j.job_id, $("#kn-log"), $("#kn-status"), loadKnowledge); } catch (e) { $("#kn-status").textContent = e.message; }
});

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

async function showChunks(path) {
  $("#kn-chunks").hidden = false; $("#kn-chunks-title").textContent = path; $("#kn-chunks-body").innerHTML = "<span class='muted'>loading…</span>";
  try { const chunks = await api(`/knowledge/chunks?path=${encodeURIComponent(path)}`);
    $("#kn-chunks-body").innerHTML = chunks.length ? chunks.map((c) => `<div class="chunk"><div class="s"><b>#${c.chunk_index}</b> · ${esc(c.breadcrumb)} · ${c.tokens} tokens</div><div class="c">${esc(c.content)}</div></div>`).join("") : "<span class='muted'>no chunks indexed for this file (run ingest)</span>";
  } catch (e) { $("#kn-chunks-body").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
$("#kn-chunks-close").addEventListener("click", () => { $("#kn-chunks").hidden = true; });
