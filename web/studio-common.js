// Shared page helpers for the Studio pages (studio.html and external.html): DOM shortcuts, the fetch wrapper,
// markdown rendering, the SSE reader and the wording of /chat/stream status phases. Loaded after trace.js.
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = TR.esc;
const api = async (path, opts = {}) => {
  const r = await fetch(new URL(path, location.origin), { credentials: "same-origin", ...opts });
  if (r.status === 401) { location.href = "/studio/login"; throw new Error("not signed in"); }
  if (!r.ok) throw new Error(`${r.status} ${await errorDetail(r)}`);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};
async function errorDetail(r) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch {} return t; }
const json = (body, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const md = (text) => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(text || "", { gfm: true, breaks: true })) : esc(text);

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

// what a /chat/stream "status" event means, while the answer bubble is still empty
function phaseText(ev) {
  return ({ choosing: "the concierge is choosing a specialist…", retrieving: `retrieving ${ev.skill_id || ""} knowledge…`, specialist: `handed over to the ${ev.skill_id || ""} specialist…`, drafting: "knowledge retrieved, writing the answer…", relaying: "specialist replied, the concierge is relaying…" })[ev.phase] || ev.phase;
}
