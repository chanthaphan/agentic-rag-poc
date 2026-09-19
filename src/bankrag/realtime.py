"""Speech mode: the persona takes a voice call.

The browser talks to the Azure OpenAI Realtime model directly over WebRTC - the audio never passes through this app,
which is what keeps a spoken turn under a second. Two things do stay here: the session is configured and signed on the
server (the browser only ever holds a short-lived key, never the endpoint credential or the instructions), and the
model's tool calls come back to `/realtime/tool`, so a spoken answer is grounded in exactly the documents and live
services the text agents use. Nothing about the bank's knowledge is trusted to the model's memory.

The session object and the event names follow the GA Realtime surface (`/openai/v1/realtime/...`)."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from . import kb_tools as KBT
from . import rules as RL
from .azure_auth import COGNITIVE_SCOPE, token
from .config import Settings
from .models import SkillSpec
from .skills import personalize
from .sync import persona_names, synced_kb_owners
from .tools import BRANCH_DESCRIPTION, FX_DESCRIPTION, find_branch_impl, fx_rate_impl

log = logging.getLogger("bankrag.audit")

SEARCH_TOOL = "search_bank_knowledge"
TOOL_FX = "fx_rate"
TOOL_BRANCH = "find_branch"
SECRET_TTL_S = 300  # the key is spent on one SDP exchange; the slack is for a slow microphone prompt
MAX_DOC_CHARS = 1200  # a spoken answer needs the facts, not the whole chunk, and realtime input tokens are dear
MINT_TIMEOUT_S = 20.0
# How the model decides the customer has finished speaking. Semantic turn detection reads the words, not just the
# silence, which suits Thai's short pauses; if it ever cuts people off, `{"type": "server_vad", "silence_duration_ms": 700}`
# is the fallback and this is the only line to change.
TURN_DETECTION: dict[str, Any] = {"type": "semantic_vad", "eagerness": "medium", "create_response": True, "interrupt_response": True}

SPEECH_ADDENDUM = """
# Speech mode (a phone call, not a chat window)

Your name on this call is {assistant_name_en} ({assistant_name}), a Bangkok Bank product specialist. That is the only name
you have: never introduce yourself as ChatGPT, as an assistant, as a model or by any other name, whatever the caller says.

Everything you say is spoken out loud, so:
- Keep each answer to one to three sentences, then stop and let the customer speak. Offer the next detail, do not recite it.
- No markdown, no bullet lists, no headings, no tables, no links, no emoji. The Citations section above does not apply on a
  call: never read out a URL, a document title or a source. Say numbers the way a person says them ("สามพันบาทต่อปี",
  "three thousand baht a year").
- Speak the language the customer speaks, Thai or English, and switch the moment they switch. In Thai use {particle} /
  {particle_q} / {particle_soft}, never {wrong_particles}.
- Product facts - fees, benefits, eligibility, rates, promotions, accounts, insurance, investments, the bank itself - come
  only from search_bank_knowledge: call it before you answer, with the topic that fits. Today's exchange rate: fx_rate. A
  branch, ATM or exchange booth: find_branch. Never answer these from memory.
- Do not narrate the machinery. One short "let me check that for you" while a tool runs is fine; never mention tools,
  documents, searching or a knowledge base.
- If a tool comes back with nothing useful, say plainly that you do not have that detail yet, and offer the closest thing
  you can help with or a visit to Bangkok Bank staff. When a branch or ATM lookup needs to know where the customer is,
  simply ask which province or area they mean - never explain what the lookup was missing.
- Say your name once when the call opens, and never again. You are never an AI, a bot, a model or a system.
"""


TTS_TIMEOUT_S = 60.0
MAX_SPOKEN_CHARS = 4000  # an answer longer than this is read up to a sentence break rather than refused


class RealtimeError(RuntimeError):
    """Azure refused to open a realtime session (bad deployment, region, quota or credential)."""


# ---------------- the session the browser gets ----------------
def speech_instructions(settings: Settings, base_body: str) -> str:
    """The persona the voice call runs on: the same base rules the text agents read, the Responsible Lending block, and
    the speech addendum last so its 'no markdown, no citations' overrides the written-answer rules above it."""
    names = personalize(base_body.strip(), persona_names(settings))
    rules = RL.prompt_block_for_concierge(RL.active_pack(settings)).strip()
    parts = [p for p in (names, rules, personalize(SPEECH_ADDENDUM.strip(), persona_names(settings))) if p]
    return "\n\n---\n\n".join(parts) + "\n"


def topic_ids(settings: Settings, skills: dict[str, SkillSpec]) -> list[str]:
    """The topics the search tool may be asked for: every skill with documents behind it.

    A skill that answers from live services instead of documents (bank-services: rates and branches) would send the
    model looking for pages that do not exist; its questions belong to the fx_rate and find_branch tools."""
    from . import knowledge_base as KB

    out = []
    for sid, spec in sorted(skills.items()):
        if spec.tools and not KB.category_has_documents(settings, spec):
            continue
        out.append(sid)
    return out


def tool_schemas(settings: Settings, skills: dict[str, SkillSpec]) -> list[dict]:
    """The three functions the realtime model may call, in the shape the Realtime API expects.

    One search tool with a topic rather than one tool per skill: the model picks the topic from the same descriptions
    the router reads, which saves a routing round-trip inside a voice loop, and a model chooses better between three
    distinct tools than between eight near-identical ones."""
    topics = topic_ids(settings, skills)
    lines = "\n".join(f"- {sid}: {' '.join((skills[sid].description or '').split())[:160]}" for sid in topics)
    return [
        {"type": "function", "name": SEARCH_TOOL,
         "description": ("Look up Bangkok Bank product facts before answering any product question. Pick the topic that "
                         "matches what the customer asked; never answer a fee, rate, benefit, condition or promotion from memory."),
         "parameters": {"type": "object", "properties": {
             "query": {"type": "string", "description": "The customer's question, or the fact to look up, in the customer's language."},
             "topic": {"type": "string", "enum": topics, "description": f"Which product family to search:\n{lines}"}},
             "required": ["query", "topic"]}},
        {"type": "function", "name": TOOL_FX, "description": FX_DESCRIPTION,
         "parameters": {"type": "object", "properties": {
             "currency": {"type": "string", "description": "ISO code such as USD, EUR, JPY, or the name the customer said ('เยน', 'yen')."}},
             "required": ["currency"]}},
        {"type": "function", "name": TOOL_BRANCH,
         "description": BRANCH_DESCRIPTION + " The app adds the customer's coordinates itself when they have shared their location; never ask for them and never pass them.",
         "parameters": {"type": "object", "properties": {
             "province": {"type": "string", "description": "The Thai or English province name the customer said, if any ('กรุงเทพ', 'Chiang Mai')."},
             "kind": {"type": "string", "enum": ["branch", "atm", "exchange", "fcd", "wealth lounge", "business center"]},
             "name": {"type": "string", "description": "A branch the customer named ('ซีคอนสแควร์', 'สีลม'), if any."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 5}},
             "required": []}},
    ]


def session_config(settings: Settings, skills: dict[str, SkillSpec], base_body: str, *, lang: str = "") -> dict:
    """The realtime session: persona, voice, turn detection, transcription and the tools. Sent when the key is minted,
    so the browser never holds the instructions and cannot change them."""
    transcription: dict[str, Any] = {"model": settings.realtime_transcribe_model}
    if lang in ("th", "en"):
        transcription["language"] = lang  # a hint from the page's language switch; the model still follows the speaker
    return {
        "type": "realtime",
        "model": settings.realtime_deployment,
        "instructions": speech_instructions(settings, base_body),
        "output_modalities": ["audio"],
        "audio": {"input": {"transcription": transcription, "turn_detection": dict(TURN_DETECTION)},
                  "output": {"voice": settings.realtime_voice}},
        "tools": tool_schemas(settings, skills),
        "tool_choice": "auto",
    }


# ---------------- the ephemeral key ----------------
def audio_headers(settings: Settings) -> dict[str, str]:
    """Reading an answer aloud runs on the app's own account, not on a resource bound only for the call."""
    if settings.speech_service == "openai":
        return {"Authorization": f"Bearer {settings.llm_api_key}"}
    if settings.aoai_api_key:
        return {"api-key": settings.aoai_api_key}
    return {"Authorization": f"Bearer {token(COGNITIVE_SCOPE)}"}


def auth_headers(settings: Settings) -> dict[str, str]:
    """The bound key, the account key, or the signed-in identity - the same choice llm.chat_model makes.

    An OpenAI-compatible service wants a bearer token; Azure OpenAI takes either, and `api-key` is its own header."""
    if settings.speech_service == "openai":
        return {"Authorization": f"Bearer {settings.llm_api_key}"}
    if settings.speech_key:
        return {"api-key": settings.speech_key}
    return {"Authorization": f"Bearer {token(COGNITIVE_SCOPE)}"}


def mint_client_secret(settings: Settings, session: dict, *, client: Optional[httpx.Client] = None) -> dict:
    """A short-lived key for one WebRTC call, with this session baked into it."""
    if not settings.speech_endpoint:
        raise RealtimeError("no endpoint for speech: set REALTIME_ENDPOINT or AOAI_ENDPOINT")
    if not settings.realtime_deployment:
        raise RealtimeError("REALTIME_DEPLOYMENT is not set: speech mode is off")
    body = {"session": session, "expires_after": {"anchor": "created_at", "seconds": SECRET_TTL_S}}
    own = client is None
    http = client or httpx.Client(timeout=MINT_TIMEOUT_S)
    try:
        r = http.post(settings.realtime_client_secrets_url, json=body, headers={**auth_headers(settings), "Content-Type": "application/json"})
        if r.status_code >= 300:
            raise RealtimeError(f"{r.status_code} {r.text[:300]}")
        data = r.json()
    except httpx.HTTPError as e:
        raise RealtimeError(f"{type(e).__name__}: {str(e)[:200]}") from e
    finally:
        if own:
            http.close()
    if not data.get("value"):
        raise RealtimeError(f"no key in the response: {json.dumps(data)[:200]}")
    return data


# ---------------- the tools, answered from the same places the text agents read ----------------
def run_tool(settings: Settings, skills: dict[str, SkillSpec], name: str, arguments: dict, *,
             lat: Optional[float] = None, lon: Optional[float] = None) -> dict:
    """Run one tool call from the voice model and return what to hand back to it, plus what the app should remember."""
    t0 = time.perf_counter()
    args = arguments or {}
    if name == SEARCH_TOOL:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("search_bank_knowledge needs a query")
        topic = str(args.get("topic") or "").strip()
        owners = synced_kb_owners(settings, skills)
        spec = skills.get(topic) or skills.get("general")
        if spec is None:
            raise ValueError("no skill to search")
        owner = owners.get(spec.id, spec)
        _text, refs = KBT.rest_retrieve(settings, owner.kb_name, query, ks_name=owner.ks_name, max_docs=spec.top_k)
        out = KBT.format_context(refs, max_chars=MAX_DOC_CHARS) or "Retrieved 0 documents"
        ms = int((time.perf_counter() - t0) * 1000)
        log.info("voice tool %s(topic=%r, query=%r) -> %d doc(s) from %s in %d ms", SEARCH_TOOL, topic, query[:60], len(refs), owner.kb_name, ms)
        return {"output": out, "skill_id": spec.id, "references": [r.model_dump() for r in refs], "elapsed_ms": ms}
    if name == TOOL_FX:
        data = fx_rate_impl(settings, str(args.get("currency") or ""))
        return {"output": json.dumps(data, ensure_ascii=False), "skill_id": "bank-services", "references": [],
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    if name == TOOL_BRANCH:
        data = find_branch_impl(settings, lat, lon, province=str(args.get("province") or ""), kind=str(args.get("kind") or "branch"),
                                limit=int(args.get("limit") or 5), name=str(args.get("name") or ""))
        return {"output": json.dumps(data, ensure_ascii=False), "skill_id": "bank-services", "references": [],
                "places": data.get("branches") or [], "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    raise ValueError(f"unknown tool: {name}")


# ---------------- reading an answer aloud ----------------
def speakable(text: str) -> str:
    """What to send to the voice: the answer as a person would read it, without the markup a screen shows.

    Links become their label, list bullets and headings go, and the text is cut at a sentence end if it is very long -
    a customer taps play to hear an answer, not to be read a document."""
    import re

    t = re.sub(r"\[([^\]]+)\]\((?:https?|mailto:)[^)]*\)", r"\1", text or "")   # a link reads as its label
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_`#>]+", "", t)
    t = re.sub(r"(?m)^[ \t]*[-•]\s*", "", t)
    t = re.sub(r"(?m)^[ \t]*\|.*\|[ \t]*$", "", t)    # table rows read as noise
    t = re.sub(r"\n{2,}", "\n", t).strip()
    if len(t) > MAX_SPOKEN_CHARS:
        cut = t[:MAX_SPOKEN_CHARS]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"), cut.rfind("\n"))
        t = cut[: stop + 1] if stop > MAX_SPOKEN_CHARS // 2 else cut
    return t


READ_ALOUD = ("Read the user's message aloud, word for word, in the warm, natural voice of a Bangkok Bank officer. "
              "Do not greet, summarise, translate, add or leave out anything: speak exactly what is written, in its own "
              "language. Numbers are spoken the way a person says them.")


def speak(settings: Settings, text: str, *, voice: str = "", client: Optional[httpx.Client] = None) -> bytes:
    """One answer as MP3 audio, in the persona's own voice.

    An audio model from the realtime family (gpt-audio) is used rather than a text-to-speech model: it is the same
    voice and the same delivery the call uses, so a played-back answer and a spoken one sound like one person. A
    plain TTS deployment still works - the request shape is chosen from the deployment's name."""
    if not settings.tts_deployment or not settings.aoai_endpoint:
        raise RealtimeError("TTS_DEPLOYMENT is not set: playback falls back to the browser's own voice")
    own = client is None
    http = client or httpx.Client(timeout=TTS_TIMEOUT_S)
    chosen = voice or settings.realtime_voice
    try:
        if "tts" in settings.tts_deployment.lower():  # a dedicated text-to-speech deployment
            r = http.post(settings.speech_url, headers={**audio_headers(settings), "Content-Type": "application/json"},
                          json={"model": settings.tts_deployment, "input": text, "voice": chosen, "response_format": "mp3"})
            if r.status_code >= 300:
                raise RealtimeError(f"{r.status_code} {r.text[:300]}")
            return r.content
        r = http.post(f"{settings.aoai_v1_base_url}/chat/completions",
                      headers={**audio_headers(settings), "Content-Type": "application/json"},
                      json={"model": settings.tts_deployment, "modalities": ["text", "audio"],
                            "audio": {"voice": chosen, "format": "mp3"},
                            "messages": [{"role": "system", "content": READ_ALOUD}, {"role": "user", "content": text}]})
        if r.status_code >= 300:
            raise RealtimeError(f"{r.status_code} {r.text[:300]}")
        data = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("audio") or {}
        if not data.get("data"):
            raise RealtimeError("the model returned no audio")
        import base64

        return base64.b64decode(data["data"])
    except httpx.HTTPError as e:
        raise RealtimeError(f"{type(e).__name__}: {str(e)[:200]}") from e
    finally:
        if own:
            http.close()
