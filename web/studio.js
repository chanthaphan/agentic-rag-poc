// Studio core: helpers, tabs, jobs, SSE reader, init. Feature modules register loaders via S.loaders.
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = TR.esc;
const api = async (path, opts = {}) => {
  const r = await fetch(new URL(path, location.origin), { credentials: "same-origin", ...opts });
  if (r.status === 401) { location.href = "/studio/login"; throw new Error("not signed in"); }
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch {} throw new Error(`${r.status} ${t}`); }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};
const json = (body, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const md = (text) => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: true })) : esc(text);
const S = { loaders: {}, models: [], skillsCache: [] };

// ---- tabs / sub-tabs ----
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  if (window.skillDirty && !confirm("You have unsaved changes in the skill editor. Leave anyway?")) return;
  $$("nav button").forEach((x) => x.classList.remove("on")); $$(".view").forEach((x) => x.classList.remove("on"));
  b.classList.add("on"); $("#view-" + b.dataset.view).classList.add("on");
  (S.loaders[b.dataset.view] || (() => {}))();
}));
function bindSubtabs(attr, prefix) {
  $$(`.subtabs button[${attr}]`).forEach((b) => b.addEventListener("click", () => {
    const group = b.parentElement; group.querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on");
    const section = group.parentElement; section.querySelectorAll(`.pane[id^='${prefix}']`).forEach((p) => p.classList.remove("on"));
    $(`#${prefix}${b.getAttribute(attr)}`).classList.add("on");
    (S.loaders[`${prefix}${b.getAttribute(attr)}`] || (() => {}))();
  }));
}
bindSubtabs("data-pane", "pane-"); bindSubtabs("data-epane", "epane-"); bindSubtabs("data-spane", "spane-");

// ---- jobs ----
async function pollJob(id, logEl, statusEl, done) {
  return new Promise((resolve) => {
    const t = setInterval(async () => {
      try {
        const j = await api(`/jobs/${id}`);
        if (logEl) { logEl.textContent = j.log.join("\n"); logEl.scrollTop = 1e9; }
        if (statusEl) statusEl.textContent = `${j.kind}: ${j.status}`;
        if (j.status !== "running") { clearInterval(t); done && done(j); resolve(j); }
      } catch (e) { clearInterval(t); if (statusEl) statusEl.textContent = e.message; resolve(null); }
    }, 1200);
  });
}

// ---- SSE ----
async function readSSE(response, onEvent) {
  const reader = response.body.getReader(); const dec = new TextDecoder(); let buf = "";
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of chunk.split("\n")) if (line.startsWith("data: ")) { let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; } onEvent(ev); }
    }
  }
}

// ---- models (live deployments) ----
async function loadModels(refresh = false) {
  try { S.models = await api(`/app/models${refresh ? "?refresh=1" : ""}`); } catch { S.models = [{ name: "gpt-4.1-mini" }]; }
  return S.models;
}
function fillModelSelect(sel, current) {
  const names = S.models.map((m) => m.name);
  if (current && !names.includes(current)) names.unshift(current);
  sel.innerHTML = names.map((n) => `<option value="${esc(n)}" ${n === current ? "selected" : ""}>${esc(n)}</option>`).join("");
}
const fmtUsd = TR.fmtUsd;

(async function authNav() { try { const r = await fetch(new URL("/.auth/me", location.origin), { credentials: "same-origin" }); if (r.ok) $("#ms-signout").hidden = false; } catch {} })();

(async function init() {
  try { const me = await api("/studio/me"); $("#who").textContent = me.tester ? `${me.tester}${me.role ? ` · ${me.role}` : ""}` : ""; if (me.role === "admin") $("#tab-access").hidden = false; } catch {}
  try { const h = await api("/health"); $("#health").textContent = `index ${h.index} · KB ${h.kb_reasoning_effort}`; } catch {}
  await loadModels();
  S.loaders.skills && S.loaders.skills();
})();
