"""Chat session: route to a skill agent, run it through the Foundry Responses API, collect citations and sources."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator, Optional

import tiktoken

from . import knowledge_base as KB
from . import router as R
from .router import usage_dict
from .pricing import load_pricing, turn_cost
from .config import Settings
from .foundry_sync import plan_kb_owners
from .foundry import project_client
from .models import Answer, Citation, RouteDecision, SessionRecord, SkillSpec

OFFTOPIC_REPLY = {
    "th": "ขออภัยค่ะ ผู้ช่วยนี้ตอบได้เฉพาะคำถามเกี่ยวกับผลิตภัณฑ์ของธนาคารกรุงเทพ เช่น บัตรเครดิต บัตรเดบิต ประกัน และการลงทุน",
    "en": "Sorry, this assistant only answers questions about Bangkok Bank products such as credit cards, debit cards, insurance and investments.",
}
MIN_CONFIDENCE = 0.5
MAX_TURNS_PER_CONVERSATION = 6  # Foundry conversations keep every tool output; rotate to cap input tokens
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sources")
REPLY_HINT = {
    "th": "Reply-language note: the customer wrote in Thai. Write the entire answer in Thai (product names may stay in English). Do not mention this note.",
    "en": "Reply-language note: the customer wrote in English. Write the entire answer in English. Do not mention this note.",
}
_HINT_ECHO_RE = re.compile(r"\s*\((?:โปรดตอบเป็นภาษาไทย|Please reply in English\.?)\)\s*")


def detect_language(text: str) -> str:
    """'th' if the message contains Thai script (Thai with English product names still counts as Thai), else 'en'."""
    thai = sum(1 for ch in text if "\u0e00" <= ch <= "\u0e7f")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if thai == 0:
        return "en"
    return "th" if thai >= 0.25 * max(1, thai + latin) else "en"


def suggestion_language(text: str) -> str:
    return detect_language(text)


class ChatSession:
    def __init__(self, settings: Settings, skills: dict[str, SkillSpec], *, project=None):
        self.settings = settings
        self.skills = skills
        self.project = project or project_client(settings)
        self.openai = self.project.get_openai_client()
        self.conversation_id: Optional[str] = None
        self.history: list[dict[str, str]] = []
        self.prev_skill: Optional[str] = None
        self.turns_in_conversation = 0
        self.kb_owners = plan_kb_owners(settings, skills)

    # ---- routing policy ----
    def decide(self, question: str, force_skill: Optional[str] = None) -> RouteDecision:
        if force_skill:
            if force_skill not in self.skills:
                raise ValueError(f"unknown skill '{force_skill}'")
            return RouteDecision(skill_id=force_skill, confidence=1.0, reason="forced by caller", language=detect_language(question))
        d = R.route(self.openai, question, self.history, self.prev_skill, self.skills)
        if d.language not in ("th", "en") or "fallback" in d.reason:
            d.language = detect_language(question)
        if d.skill_id != R.OFFTOPIC and d.confidence < MIN_CONFIDENCE and self.prev_skill in self.skills:
            d.reason = f"low confidence ({d.confidence:.2f}); staying on previous skill. {d.reason}"
            d.skill_id = self.prev_skill
        return d

    # ---- main entry ----
    def ask(self, question: str, *, force_skill: Optional[str] = None, with_sources: bool = True) -> Answer:
        """Non-streaming convenience: drains ask_stream() and returns the final Answer."""
        answer: Optional[Answer] = None
        for ev in self.ask_stream(question, force_skill=force_skill, with_sources=with_sources):
            if ev["type"] == "done":
                answer = ev["answer"]
        assert answer is not None
        return answer

    def ask_stream(self, question: str, *, force_skill: Optional[str] = None, with_sources: bool = True) -> Iterator[dict[str, Any]]:
        """Yields events: route -> delta* -> tool* -> done(answer). The Sources retrieve runs in parallel with the agent call."""
        t_start = time.perf_counter()
        decision = self.decide(question, force_skill)
        self.history.append({"role": "user", "content": question})
        if decision.skill_id == R.OFFTOPIC:
            text = OFFTOPIC_REPLY.get(decision.language, OFFTOPIC_REPLY["th"])
            self.history.append({"role": "assistant", "content": text})
            general = self.skills.get("general")
            yield {"type": "route", "skill_id": R.OFFTOPIC, "confidence": decision.confidence, "language": decision.language, "reason": decision.reason, "agent_name": ""}
            yield {"type": "delta", "text": text}
            yield {"type": "done", "answer": Answer(
                skill_id=R.OFFTOPIC, confidence=decision.confidence, route_reason=decision.reason, text=text, language=decision.language,
                suggestions=pick_suggestions(general, [], self.skills, language=decision.language) if general else [],
                trace={"timings_ms": {"route": decision.elapsed_ms, "total": int((time.perf_counter() - t_start) * 1000)}, "usage": {"router": decision.usage, "total": decision.usage},
                       "cost": turn_cost(load_pricing(self.settings), self.settings.router_model, self.settings.router_model, {"router": decision.usage}, 0)})}
            return

        spec = self.skills[decision.skill_id]
        lang = decision.language if decision.language in ("th", "en") else detect_language(question)
        yield {"type": "route", "skill_id": spec.id, "confidence": decision.confidence, "language": lang, "reason": decision.reason, "agent_name": spec.agent_name, "route_ms": decision.elapsed_ms}
        rotated = False
        if self.conversation_id is None or self.turns_in_conversation >= MAX_TURNS_PER_CONVERSATION:
            rotated = self.conversation_id is not None
            self.conversation_id = self.openai.conversations.create(items=self._recap_items() if rotated else []).id
            self.turns_in_conversation = 0
        yield {"type": "conversation", "conversation_id": self.conversation_id, "rotated": rotated}

        owner = self.kb_owners.get(spec.id, spec)
        future = None
        if with_sources and owner.id == spec.id:  # skills without documents have no knowledge base
            future = _POOL.submit(KB.retrieve, self.settings, owner.kb_name, question, ks_name=owner.ks_name, max_docs=spec.top_k)

        t_agent = time.perf_counter()
        stream = self.openai.responses.create(
            stream=True,
            conversation=self.conversation_id,
            input=[
                {"type": "message", "role": "developer", "content": REPLY_HINT[lang]},
                {"type": "message", "role": "user", "content": question},
            ],
            extra_body={"agent_reference": {"name": spec.agent_name, "type": "agent_reference"}},
        )
        final = None
        for event in stream:
            et = getattr(event, "type", "")
            if et == "response.output_text.delta":
                yield {"type": "delta", "text": event.delta}
            elif et == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "mcp_call":
                    yield {"type": "tool", "name": getattr(item, "name", ""), "arguments": (getattr(item, "arguments", "") or "")[:300], "error": str(getattr(item, "error", "") or "")}
            elif et == "response.completed":
                final = event.response
            elif et in ("response.failed", "response.incomplete", "error"):
                err = getattr(event, "response", None)
                detail = getattr(getattr(err, "error", None), "message", None) or getattr(event, "message", None) or et
                raise RuntimeError(f"agent stream {et}: {detail}")
        if final is None:
            raise RuntimeError("agent stream ended without a completed response")
        agent_ms = int((time.perf_counter() - t_agent) * 1000)
        text, citations, tool_calls, extra = parse_response(final)

        references = []
        sources_ms = 0
        if future is not None:
            t_src = time.perf_counter()
            try:
                references = future.result(timeout=60)
            except Exception as e:  # noqa: BLE001 - sources are a debugging aid, never fail the answer
                tool_calls.append({"type": "sources_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            sources_ms = int((time.perf_counter() - t_src) * 1000)  # time waited beyond the agent call
        agent_usage = usage_dict(getattr(final, "usage", None))
        trace = {
            "timings_ms": {"route": decision.elapsed_ms, "agent": agent_ms, "sources": sources_ms, "total": int((time.perf_counter() - t_start) * 1000)},
            "usage": {"router": decision.usage, "agent": agent_usage,
                      "total": {k: (decision.usage.get(k, 0) + agent_usage.get(k, 0)) for k in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")}},
            "retrieval": extra["retrieval"],
            "reasoning": extra["reasoning"],
            "model": getattr(final, "model", "") or "",
            "response_id": getattr(final, "id", "") or "",
        }
        try:
            trace["cost"] = turn_cost(load_pricing(self.settings), self.settings.router_model, trace["model"] or (spec.model or self.settings.default_chat_model),
                                      trace["usage"], extra["retrieval"]["calls"])
        except Exception as e:  # noqa: BLE001 - pricing must never break the chat
            trace["cost"] = {"error": str(e)[:120]}
        citations = resolve_citations(citations, references, lookup=lambda ids: _lookup_docs(self.settings, ids))
        self.history.append({"role": "assistant", "content": text})
        self.prev_skill = spec.id
        self.turns_in_conversation += 1
        trace["conversation"] = {"id": self.conversation_id, "turn": self.turns_in_conversation, "rotated": rotated}
        asked = [t["content"] for t in self.history if t["role"] == "user"]
        yield {"type": "done", "answer": Answer(
            skill_id=spec.id,
            confidence=decision.confidence,
            route_reason=decision.reason,
            text=strip_markers(text),
            language=lang,
            suggestions=pick_suggestions(spec, asked, self.skills, language=lang),
            citations=citations,
            references=references,
            agent_name=spec.agent_name,
            tool_calls=tool_calls,
            conversation_id=self.conversation_id or "",
            trace=trace,
            retrieval_context=extra.get("retrieval_texts", []),
        )}

    def _recap_items(self) -> list[dict[str, Any]]:
        """Seed a fresh Foundry conversation with a short recap so follow-ups keep working after rotation."""
        users = [h["content"] for h in self.history if h["role"] == "user"][-3:]
        last_answer = next((h["content"] for h in reversed(self.history) if h["role"] == "assistant"), "")
        recap = "Context from earlier in this chat (the conversation was trimmed to save tokens). Recent customer questions: " + " | ".join(users)
        if last_answer:
            recap += f"\nLast answer (abridged): {last_answer[:600]}"
        return [{"type": "message", "role": "developer", "content": recap}]

    # ---- persistence ----
    @classmethod
    def from_record(cls, settings: Settings, skills: dict[str, SkillSpec], rec: SessionRecord, *, project=None) -> "ChatSession":
        s = cls(settings, skills, project=project)
        s.conversation_id = rec.conversation_id
        s.prev_skill = rec.prev_skill if rec.prev_skill in skills else None
        s.history = [{"role": t.role, "content": t.text} for t in rec.turns]
        # count answers already in the current Foundry conversation (stored per turn since the rotation feature)
        s.turns_in_conversation = sum(1 for t in rec.turns if t.role == "assistant" and (t.trace or {}).get("conversation", {}).get("id") == rec.conversation_id) or (
            min(len([t for t in rec.turns if t.role == "assistant"]), MAX_TURNS_PER_CONVERSATION) if rec.conversation_id else 0)
        return s

    def to_record(self, rec: SessionRecord) -> SessionRecord:
        rec.conversation_id = self.conversation_id
        rec.prev_skill = self.prev_skill
        return rec

    def reset(self) -> None:
        if self.conversation_id:
            try:
                self.openai.conversations.delete(conversation_id=self.conversation_id)
            except Exception:  # noqa: BLE001
                pass
        self.conversation_id, self.history, self.prev_skill = None, [], None


_MARKER_RE = re.compile(r"【[^】]*】")


_SOURCES_HEADER_RE = re.compile(r"(?:^|\n)[ \t]*(?:[-*•]\s*)?(?:\*\*|__|#+\s*)?(?:Sources?|References?|แหล่งข้อมูล(?:อ้างอิง)?|แหล่งที่มา|ที่มา(?:ของข้อมูล)?|อ้างอิง)(?:\*\*|__)?[ \t]*[:：]?[ \t]*(?=\n|$)", re.I)
_LINKISH_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*(?:\[[^\]]*\]\([^)]*\)|https?://\S+|<https?://[^>]+>)", re.I)
_SOURCE_SENTENCE_RES = [
    # Thai: "ข้อมูลนี้อ้างอิงจาก…ค่ะ", "ข้อมูลข้างต้นมาจากเอกสาร…"
    re.compile(r"(?:(?<=\s)|^)ข้อมูล(?:นี้|ข้างต้น|ดังกล่าว|ทั้งหมด)?(?:\s*(?:อ้างอิง|นำมา|มา))?\s*จาก(?:แหล่งข้อมูล|เอกสาร|แบบ|หน้า|ฐาน|ข้อมูล)[^\n]{0,200}?(?:ค่ะ|คะ|นะคะ|ครับ|\.|(?=\n)|$)"),
    # English: "This information is based on / taken from …", "You can find the official details here [link]."
    re.compile(r"(?:(?<=\s)|^)(?:This|The above|These) (?:information|answer|details?|facts?) (?:is|are|was|were) (?:based on|from|taken from|sourced from|referenced from|according to)[^\n]{0,200}?(?:\.|(?=\n)|$)", re.I),
    re.compile(r"(?:(?<=\s)|^)(?:You can|Please) (?:find|see|check|read|refer to)[^\n]{0,60}?(?:\[[^\]]*\]\([^)]*\)|https?://\S+)[^\n]{0,40}?(?:\.|(?=\n)|$)", re.I),
]


def strip_source_talk(text: str) -> str:
    """Drop a trailing 'Sources:' / 'แหล่งข้อมูล' list and sentences that narrate where the facts came from.
    The app shows sources in its own card, and customer-facing answers must not talk about documents."""
    m = None
    for m in _SOURCES_HEADER_RE.finditer(text):
        pass
    if m is not None:
        rest = text[m.end():]
        lines = [ln for ln in rest.split("\n") if ln.strip()]
        if len(lines) <= 10 and all(_LINKISH_RE.match(ln) for ln in lines):
            text = text[: m.start()]
    for rx in _SOURCE_SENTENCE_RES:
        text = rx.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def strip_markers(text: str) -> str:
    """Remove Foundry citation markers like 【4:0†source】, any echoed reply-language hint, and source narration from the visible answer."""
    text = _HINT_ECHO_RE.sub(" ", _MARKER_RE.sub("", text))
    text = strip_source_talk(text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def pick_suggestions(spec: Optional[SkillSpec], asked: list[str], skills: dict[str, SkillSpec], n: int = 3, language: Optional[str] = None) -> list[str]:
    """Static follow-ups from the skill frontmatter in the user's language, minus what was already asked, padded from `general`."""
    if spec is None:
        return []
    asked_norm = {a.strip().lower() for a in asked}
    pool = list(spec.suggestions)
    general = skills.get("general")
    if general is not None and general.id != spec.id:
        pool += [x for x in general.suggestions if x not in pool]
    if language in ("th", "en"):
        same = [x for x in pool if suggestion_language(x) == language]
        pool = same or pool
    rotate = max(0, len(asked) - 1)
    pool = pool[rotate:] + pool[:rotate] if pool else pool
    out: list[str] = []
    for x in pool:
        if x.strip().lower() in asked_norm or x in out:
            continue
        out.append(x)
        if len(out) >= n:
            break
    return out


def _lookup_docs(settings: Settings, ids: list[str]) -> dict[str, dict]:
    from . import search_index as SI

    try:
        return SI.get_documents(settings, ids)
    except Exception:  # noqa: BLE001
        return {}


def _citation_doc_id(url: str) -> str:
    return url.split("/docs/", 1)[1].split("?", 1)[0] if "/docs/" in url else ""


def resolve_citations(citations: list[Citation], references: list, lookup=None) -> list[Citation]:
    """Search-index citations point at the search service (…/docs/<id>?…); swap in the document's real source_url.

    Ids are resolved from the direct-retrieve references first, then (if `lookup` is given) from the index itself."""
    by_id: dict[str, dict] = {r.id: {"source_url": r.source_url, "title": r.title} for r in references if getattr(r, "id", "")}
    missing = [d for d in (_citation_doc_id(c.url) for c in citations) if d and d not in by_id]
    if missing and lookup is not None:
        by_id.update(lookup(sorted(set(missing))))
    out: list[Citation] = []
    seen: set[str] = set()
    for c in citations:
        url, title = c.url, c.title
        doc = by_id.get(_citation_doc_id(url))
        if doc and doc.get("source_url"):
            url, title = doc["source_url"], doc.get("title") or title
        if url not in seen:
            seen.add(url)
            out.append(Citation(title=title, url=url))
    return out


_ENC = tiktoken.get_encoding("o200k_base")


def _tokens(text: str) -> int:
    try:
        return len(_ENC.encode(text or "", disallowed_special=()))
    except Exception:  # noqa: BLE001
        return 0


def parse_response(resp: Any) -> tuple[str, list[Citation], list[dict[str, Any]], dict[str, Any]]:
    """Returns (text, citations, tool_calls, extra) where extra = {retrieval: {...}, reasoning: [...]}."""
    text = getattr(resp, "output_text", "") or ""
    citations: list[Citation] = []
    tool_calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    retrieval = {"calls": 0, "documents": 0, "output_chars": 0, "output_tokens": 0, "query_variants": []}
    retrieval_texts: list[str] = []
    reasoning: list[str] = []
    for item in getattr(resp, "output", None) or []:
        itype = getattr(item, "type", "")
        if itype == "reasoning":
            for sm in getattr(item, "summary", None) or []:
                t = getattr(sm, "text", "") or ""
                if t:
                    reasoning.append(t)
        elif itype == "message":
            for content in getattr(item, "content", None) or []:
                for ann in getattr(content, "annotations", None) or []:
                    if getattr(ann, "type", "") == "url_citation":
                        url = getattr(ann, "url", "") or ""
                        if url and url not in seen:
                            seen.add(url)
                            citations.append(Citation(title=getattr(ann, "title", "") or "", url=url))
        elif itype == "mcp_call":
            out = getattr(item, "output", None)
            out_s = str(out) if out is not None else ""
            m = re.search(r"Retrieved (\d+) documents", out_s)
            retrieval["calls"] += 1
            retrieval["documents"] += int(m.group(1)) if m else out_s.count("【")
            retrieval["output_chars"] += len(out_s)
            retrieval["output_tokens"] += _tokens(out_s)
            if out_s.strip():
                retrieval_texts.append(out_s[:60000])
            try:
                args = json.loads(getattr(item, "arguments", "") or "{}")
                retrieval["query_variants"] += list(args.get("query_variants") or ([args["query"]] if args.get("query") else []))
            except Exception:  # noqa: BLE001
                pass
            tool_calls.append(
                {
                    "type": itype,
                    "name": getattr(item, "name", ""),
                    "arguments": (getattr(item, "arguments", "") or "")[:500],
                    "output": (str(out) if out is not None else "")[:1500],
                    "error": str(getattr(item, "error", "") or ""),
                }
            )
        elif itype == "mcp_list_tools":
            tool_calls.append({"type": itype, "tools": [getattr(t, "name", "") for t in (getattr(item, "tools", None) or [])]})
    return text, citations, tool_calls, {"retrieval": retrieval, "reasoning": reasoning, "retrieval_texts": retrieval_texts}
