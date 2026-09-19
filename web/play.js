// The play button under every answer: hear it read aloud in the persona's own voice.
//
// One answer plays at a time, and pressing play on another stops the first. The server reads the answer with the same
// voice the call uses, so playback and speech mode sound like the same person; if no voice deployment is configured
// (or the request fails) it falls back to the browser's own speech synthesis, which every phone already has.
const Play = (() => {
  const ICON_PLAY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.14v13.72c0 .78.85 1.26 1.52.86l11.2-6.86a1 1 0 0 0 0-1.72L9.52 4.28A1 1 0 0 0 8 5.14Z"/></svg>';
  const ICON_STOP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
  const ICON_WAIT = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true" class="pl-spin"><path d="M12 3a9 9 0 1 0 9 9"/></svg>';
  const cache = new Map();  // answer text -> object URL, so replaying a turn costs nothing
  let audio = null, current = null, speaking = null;

  const isThai = (s) => /[฀-๿]/.test(s || "");

  function setIcon(btn, state) {
    if (!btn) return;
    btn.innerHTML = state === "playing" ? ICON_STOP : state === "loading" ? ICON_WAIT : ICON_PLAY;
    btn.classList.toggle("on", state === "playing");
    btn.disabled = state === "loading";
  }

  function stop() {
    if (audio) { try { audio.pause(); } catch {} audio = null; }
    if (speaking) { try { speechSynthesis.cancel(); } catch {} speaking = null; }
    setIcon(current, "idle");
    current = null;
  }

  // the browser's own voice, for when the server has none configured
  function speakLocally(text, btn) {
    if (!window.speechSynthesis) { setIcon(btn, "idle"); return false; }
    const u = new SpeechSynthesisUtterance(text.slice(0, 3000));
    u.lang = isThai(text) ? "th-TH" : "en-US";
    const voice = speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith(u.lang.slice(0, 2)));
    if (voice) u.voice = voice;
    u.onend = u.onerror = () => { if (current === btn) stop(); };
    speaking = u; setIcon(btn, "playing"); current = btn;
    speechSynthesis.speak(u);
    return true;
  }

  async function play(text, btn, { tts = true } = {}) {
    if (current === btn) { stop(); return; }   // pressing play again stops it
    stop();
    text = (text || "").trim();
    if (!text) return;
    current = btn; setIcon(btn, "loading");
    let url = cache.get(text);
    if (!url && tts) {
      try {
        const r = await fetch(new URL("/tts", location.origin), {
          credentials: "same-origin", method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
        });
        if (r.ok) { url = URL.createObjectURL(await r.blob()); cache.set(text, url); }
      } catch (e) {
        console.warn("play: the server could not read it aloud, using the browser voice", e);
      }
    }
    if (current !== btn) return;               // stopped while we were fetching
    if (!url) { speakLocally(text, btn); return; }
    audio = new Audio(url);
    audio.onended = audio.onerror = () => { if (current === btn) stop(); };
    setIcon(btn, "playing");
    audio.play().catch(() => speakLocally(text, btn));
  }

  /** The button's markup. `idx` ties it to a turn so a re-render can restore which one is playing. */
  const button = (idx, label) => `<button class="play" data-play="${idx}" title="${label}" aria-label="${label}">${ICON_PLAY}</button>`;

  /** Wire every play button inside `root`; `textOf(idx)` returns the answer text of that turn. */
  function bind(root, textOf, opts = {}) {
    root.querySelectorAll("[data-play]").forEach((b) => {
      if (current && current.dataset.play === b.dataset.play) { current = b; setIcon(b, audio || speaking ? "playing" : "idle"); }
      b.addEventListener("click", (e) => { e.stopPropagation(); play(textOf(+b.dataset.play), b, opts); });
    });
  }

  return { button, bind, play, stop };
})();
