// Speech mode: a phone-style call with the assistant, with a 3D face that moves its mouth to the model's own audio.
//
// The browser talks to Azure OpenAI Realtime directly over WebRTC - audio never passes through this app, which is what
// keeps a spoken reply under a second. The app is still in charge of the three things that matter:
//   POST /realtime/session   a short-lived key with the persona, voice and tools already inside it (never the credential)
//   POST /realtime/tool      every tool the model calls, answered from the same knowledge bases the text agents read
//   POST /realtime/turns     what was said, saved as an ordinary conversation so the call is not lost when it ends
//
// Loaded as a module (TalkingHead and three need one); external.js stays a classic script and calls window.Voice.
const AVATAR_URL = new URL(`avatar.js${new URL(import.meta.url).search}`, import.meta.url);  // keep the page's ?v= cache-buster
const IDLE_MS = 5 * 60 * 1000;  // a call nobody is speaking on still bills audio tokens: hang up after this
const TRANSCRIPT_WAIT_MS = 4000;  // how long a finished answer waits for the question's transcription before saving anyway

const T = {
  th: {
    connecting: "กำลังเชื่อมต่อ…", ready: "พูดได้เลย", listening: "กำลังฟัง…", thinking: "สักครู่…", speaking: "กำลังพูด…",
    tool: "กำลังตรวจสอบข้อมูล…", ended: "วางสายแล้ว", mute: "ปิดไมค์", unmute: "เปิดไมค์", end: "วางสาย",
    micDenied: "ต้องอนุญาตให้ใช้ไมโครโฟนก่อนจึงจะคุยได้", noSupport: "เบราว์เซอร์นี้ยังใช้โหมดเสียงไม่ได้ (ใช้ Chrome, Edge หรือ Safari รุ่นใหม่)",
    failed: "เชื่อมต่อไม่สำเร็จ: ", you: "คุณ", hint: "พูดได้เลย แล้วหยุดเพื่อให้ตอบ",
  },
  en: {
    connecting: "Connecting…", ready: "Go ahead, I'm listening", listening: "Listening…", thinking: "One moment…", speaking: "Speaking…",
    tool: "Checking that…", ended: "Call ended", mute: "Mute", unmute: "Unmute", end: "End call",
    micDenied: "Microphone access is needed for a voice call", noSupport: "This browser cannot run voice mode (use a recent Chrome, Edge or Safari)",
    failed: "Could not connect: ", you: "You", hint: "Just talk, then pause for the answer",
  },
};

const $ = (s) => document.querySelector(s);
const S = {
  active: false, state: "idle", lang: "en", cfg: {}, opts: {},
  pc: null, dc: null, mic: null, avatar: null, sink: null, tap: null,
  sessionId: null, cur: null, lastUserItem: "", userText: new Map(), queue: [], caps: [],
  muted: false, idleTimer: 0, flushTimer: 0,
};
const t = (k) => (T[S.lang] || T.en)[k];

async function post(path, body) {
  const r = await fetch(new URL(path, location.origin), {
    credentials: "same-origin", method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) { let e = await r.text(); try { e = JSON.parse(e).detail || e; } catch {} throw new Error(e || r.statusText); }
  return r.json();
}

// ---------------- the panel ----------------
function setState(s, note = "") {
  S.state = s;
  const el = $("#voice-status"); if (!el) return;
  el.dataset.state = s;
  el.textContent = note || { connecting: t("connecting"), listening: S.caps.length ? t("listening") : t("ready"),
    thinking: t("thinking"), speaking: t("speaking"), tool: t("tool"), error: note || t("failed"), idle: t("ended") }[s] || "";
  if (S.avatar) S.avatar.setState(s === "speaking" ? "speaking" : s === "tool" || s === "thinking" ? "thinking" : "listening");
}

function renderCaptions() {
  const box = $("#voice-captions"); if (!box) return;
  box.innerHTML = S.caps.slice(-6).map((c) =>
    `<div class="cap ${c.who}"><b>${c.who === "user" ? esc(t("you")) : esc(S.cfg.name || "")}</b><span>${esc(c.text)}</span></div>`).join("");
  box.scrollTop = box.scrollHeight;
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function caption(who, text, { replaceLast = false } = {}) {
  const last = S.caps[S.caps.length - 1];
  if (replaceLast && last && last.who === who && last.open) { last.text = text; } else { S.caps.push({ who, text, open: true }); }
  renderCaptions();
}
function closeCaption() { const last = S.caps[S.caps.length - 1]; if (last) last.open = false; }

function touchIdle() {
  clearTimeout(S.idleTimer);
  S.idleTimer = setTimeout(() => stop(), IDLE_MS);
}

// ---------------- saving what was said ----------------
function queueTurn(turn) {
  S.queue.push(turn);
  turn.timer = setTimeout(() => flush(true), TRANSCRIPT_WAIT_MS);
  flush(false);
}

async function flush(force) {
  const ready = [];
  for (const turn of [...S.queue]) {
    const q = S.userText.get(turn.userItem);
    if (q === undefined && !force) continue;
    clearTimeout(turn.timer);
    S.queue.splice(S.queue.indexOf(turn), 1);
    const answer = turn.text || turn.streaming || "";
    if (!answer && !q) continue;  // nothing was actually said
    ready.push({ question: q || "(speech not transcribed)", answer, language: /[฀-๿]/.test(q || answer) ? "th" : "en",
                 skill_id: turn.skill_id || "voice", tool_calls: turn.tool_calls || [], references: turn.references || [], usage: turn.usage || {} });
  }
  if (!ready.length) return;
  try {
    const r = await post("/realtime/turns", { session_id: S.sessionId, turns: ready, model: S.cfg.model || "" });
    S.sessionId = r.session_id;
    if (S.opts.onSaved) S.opts.onSaved(r.session_id);
  } catch (e) {
    console.warn("voice: could not save the turn", e);
  }
}

// ---------------- the model's tool calls ----------------
const PLACE_KINDS = new Set(["branch", "atm", "exchange", "fcd", "wealth lounge", "business center"]);
function here(timeoutMs = 6000) {
  if (!navigator.geolocation) return Promise.resolve(null);
  return new Promise((resolve) => {
    let done = false; const finish = (v) => { if (!done) { done = true; resolve(v); } };
    setTimeout(() => finish(null), timeoutMs);
    navigator.geolocation.getCurrentPosition((p) => finish({ lat: p.coords.latitude, lon: p.coords.longitude }), () => finish(null),
      { enableHighAccuracy: false, timeout: timeoutMs, maximumAge: 300000 });
  });
}

async function runToolCalls(calls) {
  setState("tool");
  for (const call of calls) {
    let args = {};
    try { args = JSON.parse(call.arguments || "{}"); } catch {}
    let pos = null;
    // the model is never told the coordinates; the page adds them when the question is about a place nearby
    if (call.name === "find_branch" && !args.province && !args.name && PLACE_KINDS.has(String(args.kind || "branch"))) pos = await here();
    let output;
    try {
      const out = await post("/realtime/tool", { name: call.name, arguments: args, session_id: S.sessionId, ...(pos || {}) });
      output = out.output;
      if (S.cur) {
        S.cur.tool_calls.push({ type: "function", name: call.name, arguments: args, elapsed_ms: out.elapsed_ms });
        if (out.skill_id) S.cur.skill_id = out.skill_id;
        if ((out.references || []).length) S.cur.references = out.references;
      }
    } catch (e) {
      output = JSON.stringify({ error: String(e.message || e), say: "That lookup is not available right now." });
      if (S.cur) S.cur.tool_calls.push({ type: "function", name: call.name, arguments: args, error: String(e.message || e) });
    }
    send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: call.call_id, output } });
  }
  send({ type: "response.create" });
}

function send(msg) { if (S.dc && S.dc.readyState === "open") S.dc.send(JSON.stringify(msg)); }

// ---------------- the realtime events ----------------
function onEvent(ev) {
  switch (ev.type) {
    case "conversation.item.created":
    case "conversation.item.added":
      if (ev.item && ev.item.role === "user") S.lastUserItem = ev.item.id;
      break;
    case "input_audio_buffer.speech_started":  // barge-in: the model stops itself, we just tidy the caption
      closeCaption(); touchIdle(); setState("listening");
      break;
    case "input_audio_buffer.speech_stopped":
      setState("thinking");
      break;
    case "conversation.item.input_audio_transcription.completed":
      S.userText.set(ev.item_id, ev.transcript || "");
      caption("user", ev.transcript || "", { replaceLast: true }); closeCaption();
      flush(false);
      break;
    case "conversation.item.input_audio_transcription.failed":
      S.userText.set(ev.item_id, ""); flush(false);
      break;
    case "response.created":
      // a tool round answers in a second response: keep the same draft, so the turn we save keeps its tool calls,
      // its documents and what was already said before the lookup
      if (S.cur && S.cur.awaitingTool) { S.cur.awaitingTool = false; S.cur.streaming = ""; }
      else S.cur = { text: "", streaming: "", tool_calls: [], references: [], skill_id: "", usage: {}, userItem: S.lastUserItem };
      break;
    case "response.output_audio_transcript.delta":
      if (S.cur) { S.cur.streaming += ev.delta || ""; caption("assistant", S.cur.streaming, { replaceLast: true }); }
      break;
    case "response.output_audio_transcript.done":
      if (S.cur) {
        S.cur.streaming = ev.transcript || S.cur.streaming;
        S.cur.text = (S.cur.text ? S.cur.text + " " : "") + S.cur.streaming;
        caption("assistant", S.cur.streaming, { replaceLast: true });
        S.cur.streaming = "";
      }
      break;
    case "output_audio_buffer.started":
      setState("speaking"); touchIdle();
      break;
    case "output_audio_buffer.stopped":
    case "output_audio_buffer.cleared":
      closeCaption(); setState("listening");
      break;
    case "response.done": {
      const out = (ev.response && ev.response.output) || [];
      if (S.cur && ev.response && ev.response.usage) S.cur.usage = ev.response.usage;
      const calls = out.filter((o) => o.type === "function_call");
      if (calls.length) { if (S.cur) S.cur.awaitingTool = true; runToolCalls(calls); break; }  // the same turn continues with the answer
      // the opening greeting answers nothing, so it is not a turn to store
      if (S.cur && S.cur.userItem && (S.cur.text || S.cur.streaming)) queueTurn(S.cur);
      S.cur = null;
      break;
    }
    case "error":
      console.warn("voice: realtime error", ev);
      if (ev.error && ev.error.message) setState("error", (T[S.lang] || T.en).failed + ev.error.message);
      break;
  }
}

// ---------------- the call ----------------
async function start(opts = {}) {
  if (S.active) return;
  S.opts = opts; S.cfg = opts.config || {}; S.lang = opts.lang === "th" ? "th" : "en";
  S.caps = []; S.queue = []; S.userText = new Map(); S.cur = null; S.sessionId = opts.sessionId || null; S.muted = false;
  const panel = $("#voice"); panel.hidden = false; S.active = true;
  document.body.classList.add("voice-open");
  renderCaptions(); setState("connecting");
  $("#voice-name").textContent = S.cfg.name || "";
  $("#voice-hint").textContent = t("hint");
  $("#voice-mute").textContent = t("mute"); $("#voice-end").textContent = t("end");

  if (!navigator.mediaDevices || !window.RTCPeerConnection) { setState("error", t("noSupport")); return; }

  // The face is drawn FIRST, and on its own: it must not depend on the microphone, the key or the connection. A
  // refused microphone used to leave the panel completely empty, which reads as "the avatar is broken" when in fact
  // nothing had been drawn yet. Now the face is there while the rest connects, and stays if the call cannot start.
  const avatarReady = (async () => {
    try {
      const { createAvatar } = await import(AVATAR_URL);
      S.avatar = await createAvatar($("#voice-avatar"), {
        url: opts.avatarUrl || "", gender: opts.gender || "male",
        accent: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#1f5fd6",
      });
      if (S.avatar.audioCtx.state !== "running") await S.avatar.audioCtx.resume().catch(() => {});
    } catch (e) {
      console.warn("voice: the avatar could not be drawn, the call continues with audio only", e);
      $("#voice-fallback").hidden = false;
    }
  })();

  // the microphone next: the ephemeral key is short-lived, so it is minted only once the permission dialog is done
  const micReady = navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
    .then((stream) => { S.mic = stream; })
    .catch((e) => { console.warn("voice: microphone", e); });
  await Promise.all([avatarReady, micReady]);
  if (!S.active) return;  // hung up while we were asking
  if (!S.mic) { setState("error", t("micDenied")); return; }

  try {
    const sess = await post("/realtime/session", { lang: S.lang });
    S.cfg.model = sess.model;

    S.pc = new RTCPeerConnection();
    S.pc.ontrack = (e) => {
      if (e.track.kind !== "audio") return;
      // The <audio> element plays the call; Web Audio only listens in, to read the mouth shapes off the signal.
      // Playing through Web Audio instead is what makes iPhones crackle: Safari mishandles a remote WebRTC track fed
      // into an AudioContext, especially while the microphone holds the audio session in voice-chat mode. A media
      // element has none of that trouble, and an analyser tap that glitches costs nothing but a slightly noisier mouth.
      S.sink = $("#voice-sink");
      S.sink.srcObject = e.streams[0];
      S.sink.muted = false; S.sink.volume = 1; S.sink.playsInline = true;
      S.sink.play().catch((err) => console.warn("voice: playback needs a tap", err));
      // the analyser gets its own CLONE of the track: a track already feeding a playing media element delivers
      // nothing to a Web Audio source node, and the mouth then stays shut through the whole answer
      try {
        S.tap = new MediaStream([e.track.clone()]);
        if (S.avatar) S.avatar.attachStream(S.tap);
      } catch (err) {
        console.warn("voice: no separate tap for the mouth", err);
        if (S.avatar) S.avatar.attachStream(e.streams[0]);
      }
    };
    S.pc.addTrack(S.mic.getAudioTracks()[0], S.mic);
    S.dc = S.pc.createDataChannel("oai-events");
    S.dc.addEventListener("message", (m) => { try { onEvent(JSON.parse(m.data)); } catch {} });
    S.dc.addEventListener("open", () => {
      setState("listening");
      const who = S.cfg.name || "";  // the persona's name comes from the app's settings, never from this file
      send({ type: "response.create", response: { instructions: S.lang === "th"
        ? `ทักทายลูกค้าสั้น ๆ หนึ่งประโยค${who ? ` แนะนำตัวว่าชื่อ ${who}` : ""} จากธนาคารกรุงเทพ แล้วถามว่าจะให้ช่วยเรื่องอะไร`
        : `Greet the customer in one short sentence${who ? `, say your name is ${who}` : ""}, say you are from Bangkok Bank, and ask what they would like help with.` } });
    });
    S.pc.addEventListener("connectionstatechange", () => {
      if (["failed", "disconnected"].includes(S.pc.connectionState) && S.active) setState("error", t("failed") + S.pc.connectionState);
    });
    await S.pc.setLocalDescription(await S.pc.createOffer());
    const r = await fetch(sess.calls_url, {
      method: "POST", headers: { Authorization: `Bearer ${sess.client_secret}`, "Content-Type": "application/sdp" },
      body: S.pc.localDescription.sdp,
    });
    if (!r.ok) throw new Error(`SDP ${r.status}: ${(await r.text()).slice(0, 200)}`);
    await S.pc.setRemoteDescription({ type: "answer", sdp: await r.text() });
    touchIdle();
  } catch (e) {
    console.error("voice: could not start the call", e);
    setState("error", t("failed") + (e.message || e));
  }
}

async function stop() {
  if (!S.active) return;
  S.active = false;
  clearTimeout(S.idleTimer);
  try { S.dc && S.dc.close(); } catch {}
  try { S.pc && S.pc.close(); } catch {}
  try { S.mic && S.mic.getTracks().forEach((x) => x.stop()); } catch {}
  if (S.sink) { try { S.sink.pause(); } catch {} S.sink.srcObject = null; }
  if (S.tap) { try { S.tap.getTracks().forEach((t) => t.stop()); } catch {} S.tap = null; }
  if (S.avatar) { try { S.avatar.dispose(); } catch {} S.avatar = null; }
  S.pc = S.dc = S.mic = null;
  if (S.cur && S.cur.userItem && (S.cur.text || S.cur.streaming)) { S.cur.text = S.cur.text || S.cur.streaming; queueTurn(S.cur); }
  S.cur = null;
  await flush(true);
  $("#voice").hidden = true; $("#voice-fallback").hidden = true;
  document.body.classList.remove("voice-open");
  setState("idle");
  if (S.opts.onEnd) S.opts.onEnd(S.sessionId);
}

function toggleMute() {
  if (!S.mic) return;
  S.muted = !S.muted;
  S.mic.getAudioTracks().forEach((x) => { x.enabled = !S.muted; });
  $("#voice-mute").textContent = S.muted ? t("unmute") : t("mute");
  $("#voice-mute").classList.toggle("on", S.muted);
}

if ($("#voice")) {
  $("#voice-end").addEventListener("click", () => stop());
  $("#voice-mute").addEventListener("click", toggleMute);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.active) stop(); });
}
window.Voice = { start, stop, toggleMute, get active() { return S.active; }, get avatar() { return S.avatar; } };
window.dispatchEvent(new Event("voice-ready"));
