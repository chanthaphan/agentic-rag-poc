"""Chat session: route to a skill agent, run it through the Foundry Responses API, collect citations and sources."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator, Optional

import logging

import tiktoken

from . import knowledge_base as KB
from . import router as R
from . import rules as RL
from .router import usage_dict
from .pricing import load_pricing, turn_cost
from .config import Settings
from . import search_index as SI
from .foundry_sync import synced_kb_owners
from .foundry import project_client
from .models import Answer, Citation, RouteDecision, SessionRecord, SkillSpec

OFFTOPIC_REPLY = {
    "th": "ขออภัยค่ะ ผู้ช่วยนี้ตอบได้เฉพาะคำถามเกี่ยวกับผลิตภัณฑ์ของธนาคารกรุงเทพ เช่น บัตรเครดิต บัตรเดบิต ประกัน และการลงทุน",
    "en": "Sorry, this assistant only answers questions about Bangkok Bank products such as credit cards, debit cards, insurance and investments.",
}
MIN_CONFIDENCE = 0.5
MAX_TURNS_PER_CONVERSATION = 6  # Foundry conversations keep every tool output; rotate to cap input tokens
audit = logging.getLogger("bankrag.audit")
log = logging.getLogger("bankrag.chat")
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sources")
REPLY_HINT = {
    "th": "Reply-language note: the customer wrote in Thai. Write the entire answer in Thai (product names may stay in English). Do not mention this note.",
    "en": "Reply-language note: the customer wrote in English. Write the entire answer in English. Do not mention this note.",
}
_HINT_ECHO_RE = re.compile(r"\s*\((?:โปรดตอบเป็นภาษาไทย|Please reply in English\.?)\)\s*")
# The coordinates ride along with the question (see question_with_location); if an agent ever echoes that line back,
# the customer must not read their own latitude out of a chat bubble.
_LOCATION_ECHO_RE = re.compile(r"\s*\[\s*customer location:[^\]]*\]\s*", re.I)


def location_note(location: tuple[float, float]) -> str:
    """The customer's coordinates as a developer note, so a branch tool call can use them.

    The customer shared these deliberately for this question; they are not stored with the turn."""
    lat, lon = location
    return (f"Customer location note: the customer is at latitude {lat:.6f}, longitude {lon:.6f}. "
            "Use these coordinates when a tool needs a position (nearest branch, where to exchange money). "
            "Never read the coordinates out to the customer and never mention this note.")


def places_for_map(settings, text: str, location: Optional[tuple[float, float]], question: str = "") -> list[dict[str, Any]]:
    """The places the answer named, as map-ready dicts - empty whenever anything is missing or goes wrong.

    A pin is a nicety: it must never delay or break an answer that is already correct, so every failure here is
    swallowed and the customer simply gets the text they would have had anyway.
    """
    from . import services as SV

    from . import provinces as PROV

    try:
        lat, lon = location if location else (None, None)
        # Without coordinates the locator still needs somewhere to look, and both the question and the answer say where
        # the customer means - the answer repeats it in every address it quotes.
        province = "" if location else (PROV.province_in(text) or PROV.province_in(question))
        if not location and not province:
            return []
        found = SV.places_mentioned(settings, text, lat=lat, lon=lon, province=province)
        return [{**{k: v for k, v in vars(p).items() if v not in (None, "", [])}, "maps_url": SV.maps_url(p)}
                for p in found if p.lat is not None and p.lon is not None]
    except Exception as e:  # noqa: BLE001
        log.info("places_for_map: skipped (%s: %s)", type(e).__name__, e)
        return []


def question_with_location(question: str, location: Optional[tuple[float, float]]) -> str:
    """The question with the coordinates attached to it, for handoff mode.

    A developer note reaches the concierge, but the specialist that owns the branch lookup only ever sees the message
    the concierge chooses to send it - and a model asked to copy numbers across a handoff sometimes does not. Attached
    to the question itself, the coordinates travel with the one thing the concierge is told to relay verbatim.
    """
    if not location:
        return question
    lat, lon = location
    return (f"{question}\n[customer location: latitude {lat:.6f}, longitude {lon:.6f} - pass this line on to the "
            "specialist; it is not part of what the customer said and must never be shown to them]")


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
        self.last_location: Optional[tuple[float, float]] = None  # so a later turn can still put a pin on the map
        self.history: list[dict[str, str]] = []
        self.prev_skill: Optional[str] = None
        self.turns_in_conversation = 0
        # the knowledge base each agent really got at the last sync, so Sources look where the agent retrieves from
        self.kb_owners = synced_kb_owners(settings, skills)

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

    def ask_stream(self, question: str, *, force_skill: Optional[str] = None, with_sources: bool = True,
                   location: Optional[tuple[float, float]] = None) -> Iterator[dict[str, Any]]:
        """Yields events: route -> delta* -> tool* -> done(answer). The Sources retrieve runs in parallel with the agent call."""
        # The browser asks for a position only when the question looks like a place question, and the customer can
        # refuse or the prompt can time out. Where they were a moment ago is still where they are, so keep it for the
        # map pin - it is never sent to an agent, only used to look a branch back up.
        if location:
            self.last_location = location
        map_location = location or self.last_location
        t_start = time.perf_counter()
        if self.settings.orchestration_mode == "a2a" and not force_skill:
            yield from self._ask_concierge(question, t_start, with_sources=with_sources, location=location)
            return
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
        future = self._sources_future(spec, owner, question) if with_sources else None
        yield {"type": "status", "phase": "retrieving" if future is not None else "drafting", "skill_id": spec.id}

        t_agent = time.perf_counter()
        stream = self.openai.responses.create(
            stream=True,
            conversation=self.conversation_id,
            input=[
                {"type": "message", "role": "developer", "content": REPLY_HINT[lang]},
                *([{"type": "message", "role": "developer", "content": location_note(location)}] if location else []),
                {"type": "message", "role": "user", "content": question},
            ],
            extra_body={"agent_reference": {"name": spec.agent_name, "type": "agent_reference"}},
        )
        final = None
        for event in stream:
            et = getattr(event, "type", "")
            if et == "response.output_text.delta":
                yield {"type": "delta", "text": event.delta}
            elif et == "response.output_item.added":
                if getattr(getattr(event, "item", None), "type", "") == "mcp_call":
                    yield {"type": "status", "phase": "retrieving", "skill_id": spec.id}
            elif et == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "mcp_call":
                    yield {"type": "tool", "name": getattr(item, "name", ""), "arguments": (getattr(item, "arguments", "") or "")[:300], "error": str(getattr(item, "error", "") or "")}
                    yield {"type": "status", "phase": "drafting", "skill_id": spec.id}
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
        text, appended = self._apply_rules(strip_markers(text), question, lang, spec.id, trace)
        if appended:
            yield {"type": "delta", "text": appended}
        self.history.append({"role": "assistant", "content": text})
        self.prev_skill = spec.id
        self.turns_in_conversation += 1
        trace["conversation"] = {"id": self.conversation_id, "turn": self.turns_in_conversation, "rotated": rotated}
        asked = [t["content"] for t in self.history if t["role"] == "user"]
        yield {"type": "done", "answer": Answer(
            skill_id=spec.id,
            confidence=decision.confidence,
            route_reason=decision.reason,
            text=text,
            language=lang,
            suggestions=suggestions_for(self.settings, self.openai, spec, asked, self.skills,
                                        question=question, answer=text, language=lang, trace=trace),
            citations=citations,
            references=references,
            agent_name=spec.agent_name,
            tool_calls=tool_calls,
            places=places_for_map(self.settings, text, map_location, question),
            conversation_id=self.conversation_id or "",
            trace=trace,
            retrieval_context=extra.get("retrieval_texts", []),
        )}

    def _sources_future(self, spec, owner, question: str):
        """Start the Sources lookup in the background: the skill's own knowledge base when it has one, otherwise a
        hybrid index search filtered to the skill's knowledge space (skills on the shared base, e.g. on the free tier)."""
        if owner.id == spec.id:
            return _POOL.submit(KB.retrieve, self.settings, owner.kb_name, question, ks_name=owner.ks_name, max_docs=spec.top_k)
        if spec.product_category in ("all", "*", ""):
            return None
        return _POOL.submit(_index_references, self.settings, question, spec.product_category, spec.top_k)

    def _ask_concierge(self, question: str, t_start: float, *, with_sources: bool = True,
                       location: Optional[tuple[float, float]] = None) -> Iterator[dict[str, Any]]:
        """Handoff mode: the bank-concierge agent picks a specialist and calls it over A2A inside Foundry (no local router)."""
        from .foundry_native import CONCIERGE_AGENT

        # only the live position is sent to an agent; a remembered one is good enough to look a named branch back up
        map_location = location or self.last_location
        lang = detect_language(question)
        self.history.append({"role": "user", "content": question})
        yield {"type": "route", "skill_id": "concierge", "confidence": 1.0, "language": lang, "reason": "handoff: the concierge agent chooses the specialist over A2A", "agent_name": CONCIERGE_AGENT, "route_ms": 0}
        rotated = False
        if self.conversation_id is None or self.turns_in_conversation >= MAX_TURNS_PER_CONVERSATION:
            rotated = self.conversation_id is not None
            self.conversation_id = self.openai.conversations.create(items=self._recap_items() if rotated else []).id
            self.turns_in_conversation = 0
        yield {"type": "conversation", "conversation_id": self.conversation_id, "rotated": rotated}
        yield {"type": "status", "phase": "choosing", "skill_id": ""}
        t_agent = time.perf_counter()
        stream = self.openai.responses.create(
            stream=True, conversation=self.conversation_id,
            input=[{"type": "message", "role": "developer", "content": REPLY_HINT[lang]},
                   *([{"type": "message", "role": "developer", "content": location_note(location)}] if location else []),
                   {"type": "message", "role": "user", "content": question_with_location(question, location)}],
            extra_body={"agent_reference": {"name": CONCIERGE_AGENT, "type": "agent_reference"}},
        )
        final = None
        future = None  # Sources retrieve for the specialist's knowledge base, started as soon as the concierge picks one
        t_src_start = None
        for event in stream:
            et = getattr(event, "type", "")
            if et == "response.output_text.delta":
                yield {"type": "delta", "text": event.delta}
            elif et == "response.output_item.added":
                item = getattr(event, "item", None)
                if str(getattr(item, "type", "")) == "a2a_preview_call":
                    yield {"type": "status", "phase": "specialist", "skill_id": _a2a_skill_id(item)}
            elif et == "response.output_item.done":
                item = getattr(event, "item", None)
                itype = str(getattr(item, "type", ""))
                if itype.startswith("a2a"):
                    yield {"type": "tool", "name": itype, "arguments": _a2a_summary(item)[:300], "error": str(getattr(item, "error", "") or "")}
                    if itype == "a2a_preview_call":
                        yield {"type": "status", "phase": "specialist", "skill_id": _a2a_skill_id(item)}
                    elif itype == "a2a_preview_call_output":
                        yield {"type": "status", "phase": "relaying", "skill_id": _a2a_skill_id(item)}
                    if itype == "a2a_preview_call" and future is None and with_sources:
                        f = _a2a_fields(item)
                        sid = str(f.get("name", ""))[4:] if str(f.get("name", "")).startswith("a2a-") else ""
                        delegated = _a2a_question(str(f.get("arguments") or "")) or question
                        target = self.skills.get(sid)
                        owner = self.kb_owners.get(sid, target) if target else None
                        if target is not None and owner is not None:
                            t_src_start = time.perf_counter()
                            future = self._sources_future(target, owner, delegated)
            elif et == "response.completed":
                final = event.response
            elif et in ("response.failed", "response.incomplete", "error"):
                err = getattr(event, "response", None) or event
                detail = getattr(getattr(err, "error", None), "message", None) or getattr(event, "message", None) or et
                raise RuntimeError(f"concierge stream {et}: {detail}")
        if final is None:
            raise RuntimeError("concierge stream ended without a completed response")
        agent_ms = int((time.perf_counter() - t_agent) * 1000)
        text, citations, tool_calls, extra = parse_response(final)
        specialist = _a2a_specialist(tool_calls, self.skills)
        spec = self.skills.get(specialist)
        references: list = []
        sources_ms = 0
        if future is not None:
            t_wait = time.perf_counter()
            try:
                references = future.result(timeout=60)
            except Exception as e:  # noqa: BLE001 - sources are a debugging aid, never fail the answer
                tool_calls.append({"type": "sources_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            sources_ms = int((time.perf_counter() - t_wait) * 1000)
        # the specialist's citation markers 【n:m†title】 travel inside the A2A output; the concierge's relay drops them
        citations = _citations_from_markers(_a2a_outputs(tool_calls), references, lookup_title=lambda title: _lookup_title(self.settings, title, spec.product_category if spec else None)) or citations
        extra["retrieval"]["documents"] = max(int(extra["retrieval"].get("documents") or 0), len(references))
        agent_usage = usage_dict(getattr(final, "usage", None))
        trace = {
            "timings_ms": {"route": 0, "agent": agent_ms, "sources": sources_ms, "total": int((time.perf_counter() - t_start) * 1000)},
            "usage": {"router": {}, "agent": agent_usage, "total": agent_usage},
            "retrieval": extra["retrieval"], "reasoning": extra["reasoning"], "model": getattr(final, "model", "") or "", "response_id": getattr(final, "id", "") or "",
            "handoff": {"mode": "a2a", "concierge": CONCIERGE_AGENT, "specialist": specialist, "calls": [c for c in tool_calls if str(c.get("type", "")).startswith("a2a")],
                        "usage_pending": bool(specialist != "concierge" and self.settings.appinsights_app_id)},
        }
        try:
            trace["cost"] = turn_cost(load_pricing(self.settings), self.settings.router_model, trace["model"] or (self.settings.concierge_model or self.settings.default_chat_model), trace["usage"], 0)
            trace["cost"]["note"] = ("concierge tokens; the specialist's tokens are read from the Foundry trace a few minutes later" if trace["handoff"]["usage_pending"]
                                     else "concierge tokens only; connect Application Insights (APPINSIGHTS_APP_ID) to add the specialist's tokens")
        except Exception as e:  # noqa: BLE001
            trace["cost"] = {"error": str(e)[:120]}
        citations = resolve_citations(citations, references, lookup=lambda ids: _lookup_docs(self.settings, ids))
        text, appended = self._apply_rules(strip_markers(text), question, lang, specialist, trace)
        if appended:
            yield {"type": "delta", "text": appended}
        self.history.append({"role": "assistant", "content": text})
        self.prev_skill = specialist if specialist in self.skills else self.prev_skill
        self.turns_in_conversation += 1
        trace["conversation"] = {"id": self.conversation_id, "turn": self.turns_in_conversation, "rotated": rotated}
        asked = [h["content"] for h in self.history if h["role"] == "user"]
        yield {"type": "done", "answer": Answer(
            skill_id=specialist, confidence=1.0, route_reason=f"concierge handed off to {spec.agent_name if spec else 'no specialist'} over A2A" if spec else "concierge answered without a handoff",
            text=text, language=lang,
            suggestions=suggestions_for(self.settings, self.openai, spec or self.skills.get("general"), asked, self.skills,
                                        question=question, answer=text, language=lang, trace=trace),
            citations=citations, references=references, agent_name=CONCIERGE_AGENT, tool_calls=tool_calls,
            places=places_for_map(self.settings, text, map_location, question), conversation_id=self.conversation_id or "", trace=trace,
            retrieval_context=(extra.get("retrieval_texts") or []) + _a2a_outputs(tool_calls),
        )}

    def _apply_rules(self, text: str, question: str, language: str, skill_id: str, trace: dict[str, Any]) -> tuple[str, str]:
        """Responsible Lending guard on the drafted answer: missing mandatory warnings are appended verbatim (returned
        so a streaming caller can emit them) and the findings go on the trace. A broken rule pack must not take the
        chat down, so a failure is recorded on the turn instead of raised - it is visible in the trace and in Studio."""
        try:
            text, report = RL.guard(self.settings, text, question=question, language=language, skill_id=skill_id)
        except Exception as e:  # noqa: BLE001
            trace["compliance"] = {"error": f"{type(e).__name__}: {str(e)[:200]}", "checked": 0}
            return text, ""
        if report:
            trace["compliance"] = {k: v for k, v in report.items() if k != "appended"}
            if report.get("fixed") or report.get("violations"):
                audit.info("responsible-lending skill=%s products=%s appended=%s violations=%s", skill_id,
                           report.get("products"), report.get("fixed"), report.get("violations"))
        return text, report.get("appended", "")

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


# Narration that is a CLAUSE, not a sentence: "สำหรับ SCB อ่านจากเอกสารที่เกรสได้ดู ยังไม่มีข้อมูลเปรียบเทียบ…". Dropping the
# whole sentence would take the answer with it, so only the clause goes. Thai puts spaces between phrases even though
# it has none between words, so such a clause is one whitespace-delimited token - which is what makes this safe.
# "เอกสารแนบ" / "เอกสารสัญญา" are the customer's own paperwork and the regulator's annexes - real things, not our
# retrieval. The mandated warnings are appended after this runs, but the model can quote a clause in its own words too.
_REAL_PAPERWORK = r"(?!แนบ|สัญญา|ประกอบ|สมัคร|ยืนยัน|สิทธิ)"
_SOURCE_CLAUSE_RES = [
    re.compile(r"(?:(?<=\s)|^)(?:อ่าน|ดู|ค้น|เช็ค|ตรวจ|หา)?(?:จาก|ตาม|ใน|เท่าที่)"
               r"\S*?(?:เอกสาร" + _REAL_PAPERWORK + r"|ฐานข้อมูล|แหล่งข้อมูล|ฐานความรู้|ข้อมูลที่มี|ข้อมูลที่ได้|ที่ค้น|ที่เห็น|ที่ได้ดู)\S*"
               r"(?=[\s,.;:ๆ]|$)"),
    re.compile(r"(?:(?<=\s)|^)(?:เท่าที่|ตามที่)\S*(?:ค้น|หา|ดู|มีอยู่)\S*(?=[\s,.;:]|$)"),
    re.compile(r",?\s*(?:based on|from|in|according to) (?:the |my |our )?"
               r"(?:documents?|materials?|sources?|records?|knowledge base|information)(?: (?:I|we) (?:have|can see|found|looked at|retrieved))?"
               r"(?=[\s,.;:]|$)", re.I),
]
# The same narration as the opening clause of an English sentence, which leaves the rest starting with ", the …".
_LEAD_SOURCE_EN_RE = re.compile(
    r"(^|\n)(?:based on|according to|from) (?:the |my |our )?"
    r"(?:documents?|materials?|sources?|records?|knowledge base|information)(?: (?:I|we) (?:have|can see|found|looked at|retrieved))?"
    r"[ \t]*,[ \t]*([a-z])", re.I)


def strip_source_talk(text: str) -> str:
    """Drop a trailing 'Sources:' / 'แหล่งข้อมูล' list and the places an answer narrates where its facts came from.
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
    text = _LEAD_SOURCE_EN_RE.sub(lambda m: m.group(1) + m.group(2).upper(), text)
    for rx in _SOURCE_CLAUSE_RES:
        text = rx.sub("", text)
    # tidy what the removal left: a doubled space, a space before punctuation, a line now opening with its old comma
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    text = re.sub(r"(^|\n)[ \t]*[,;:][ \t]*", r"\1", text)
    text = re.sub(r"(^|\n)[ \t]+", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def strip_markers(text: str) -> str:
    """Remove Foundry citation markers like 【4:0†source】, any echoed reply-language hint, and source narration from the visible answer."""
    text = _LOCATION_ECHO_RE.sub(" ", _HINT_ECHO_RE.sub(" ", _MARKER_RE.sub("", text)))
    text = strip_source_talk(text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


SUGGEST_PROMPT = (
    "You write the three follow-up questions a Bangkok Bank customer would most likely tap next, in {lang_name}.\n"
    "Base them on THIS exchange, not on the product catalogue: follow the thread the customer is actually on, and go "
    "one step further than the answer already went (a condition it mentioned but did not detail, the next step to take "
    "it up, the obvious comparison, the thing the answer said to check).\n"
    "Rules: each is a question the customer asks the bank, first person, 4-12 words, no numbering, no quotes. "
    "Do not repeat a question already asked. Do not ask something the answer already fully answered. "
    "If the answer said there were no details on something, do not ask that same thing again.\n"
    "Reply as a JSON array of exactly 3 strings and nothing else."
)


def dynamic_suggestions(openai_client, question: str, answer: str, language: str, model: str,
                        asked: Optional[list[str]] = None) -> list[str]:
    """Follow-ups written from the turn that just happened, so they track the conversation instead of the catalogue.

    Returns [] on any failure or timeout: the caller falls back to the skill's static list, because a missing chip row
    is a much smaller problem than a slow or broken answer."""
    lang_name = "Thai" if language == "th" else "English"
    already = "\n".join(f"- {a}" for a in (asked or [])[-5:])
    try:
        resp = openai_client.responses.create(
            model=model,
            input=[
                {"type": "message", "role": "developer", "content": SUGGEST_PROMPT.format(lang_name=lang_name)},
                {"type": "message", "role": "user",
                 "content": f"Customer asked: {question}\n\nAssistant answered:\n{answer[:2500]}"
                            + (f"\n\nAlready asked earlier (do not repeat):\n{already}" if already else "")},
            ],
            max_output_tokens=200,
        )
        raw = (getattr(resp, "output_text", "") or "").strip()
    except Exception:  # noqa: BLE001 - suggestions are a nicety; never fail the answer for them
        return []
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[str] = []
    seen = {a.strip().lower() for a in (asked or [])}
    for x in items:
        s = str(x).strip().strip('"').lstrip("-•").strip()
        if 3 < len(s) <= 120 and s.lower() not in seen and s not in out:
            out.append(s)
    return out[:3]


def suggestions_for(settings: Settings, openai_client, spec, asked: list[str], skills: dict, *, question: str,
                    answer: str, language: str, trace: dict[str, Any]) -> list[str]:
    """Dynamic follow-ups when SUGGESTIONS_MODE allows it, with the skill's static list as the fallback."""
    static = pick_suggestions(spec, asked, skills, language=language) if spec else []
    if settings.suggestions_mode != "dynamic" or not answer.strip():
        trace["suggestions"] = {"mode": "static"}
        return static
    t0 = time.perf_counter()
    model = settings.suggestions_model or settings.router_model
    dyn = dynamic_suggestions(openai_client, question, answer, language, model, asked)
    trace["suggestions"] = {"mode": "dynamic" if dyn else "static (dynamic returned nothing)",
                            "model": model, "ms": int((time.perf_counter() - t0) * 1000)}
    return dyn or static


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
        elif itype.startswith("a2a"):
            tool_calls.append({"type": itype, **_a2a_fields(item)})
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



# ---------------- A2A helpers ----------------
_A2A_SKIP = {"type", "id", "status"}


def _index_references(settings: Settings, question: str, category: str, top_k: int) -> list[KB.Reference]:
    """Sources for a skill without its own knowledge base: best chunk per document from the shared index, filtered by space."""
    hits = SI.hybrid_search(settings, question, category=category, k=max(top_k * 3, 6))
    refs: list[KB.Reference] = []
    seen: set[str] = set()
    for h in hits:
        key = str(h.get("doc_id") or h.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        refs.append(KB.Reference(id=key, title=str(h.get("title") or ""), source_url=str(h.get("source_url") or ""), product_name=str(h.get("product_name") or ""),
                                 snippet=str(h.get("snippet") or ""), score=h.get("reranker_score") if h.get("reranker_score") is not None else h.get("score")))
        if len(refs) >= top_k:
            break
    return refs


def _a2a_fields(item: Any) -> dict[str, Any]:
    """Flatten the useful attributes of an a2a_preview_call / a2a_preview_call_output item (shape is preview and may change)."""
    out: dict[str, Any] = {}
    try:
        src = item.model_dump() if hasattr(item, "model_dump") else dict(getattr(item, "__dict__", {}))  # model_dump keeps the preview-only extras (name, arguments, output)
    except Exception:  # noqa: BLE001
        src = dict(getattr(item, "__dict__", {}))
    for k, v in src.items():
        if k.startswith("_") or k in _A2A_SKIP or v is None or k in ("content", "role", "phase", "agent_reference", "response_id"):
            continue
        s = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False, default=str)
        out[k] = s[:4000] if isinstance(s, str) else s
    out["id"] = getattr(item, "id", "")
    out["status"] = getattr(item, "status", "")
    return out


def _a2a_skill_id(item: Any) -> str:
    """Skill id behind an A2A item: the connection is named a2a-<skill id>."""
    name = str(_a2a_fields(item).get("name") or "")
    return name[4:] if name.startswith("a2a-") else ""


def _a2a_summary(item: Any) -> str:
    f = _a2a_fields(item)
    return f"{f.get('name', '')}: " + str(f.get("arguments") or f.get("output") or "")[:200]


def _a2a_specialist(tool_calls: list[dict[str, Any]], skills: dict[str, SkillSpec]) -> str:
    """Which skill agent the concierge called: match agent / connection names inside the A2A items."""
    for c in tool_calls:
        if str(c.get("type", "")).startswith("a2a") and str(c.get("name", "")).startswith("a2a-") and c["name"][4:] in skills:
            return c["name"][4:]
    blob = " ".join(str(v) for c in tool_calls if str(c.get("type", "")).startswith("a2a") for v in c.values())
    for spec in sorted(skills.values(), key=lambda s: -len(s.id)):
        if spec.agent_name in blob or f"a2a-{spec.id}" in blob:
            return spec.id
    return "concierge"


def _a2a_outputs(tool_calls: list[dict[str, Any]]) -> list[str]:
    """The specialists' answers as returned over A2A: the concierge's grounding context (used by the quality evals)."""
    return [str(c["output"]) for c in tool_calls if c.get("type") == "a2a_preview_call_output" and c.get("output")]


_CITE_MARKER_RE = re.compile(r"【\d+:\d+†([^】]+)】")


def _a2a_question(arguments: str) -> str:
    """The text the concierge delegated: {"message": {"parts": [{"kind": "text", "text": ...}]}}."""
    try:
        parts = json.loads(arguments).get("message", {}).get("parts", [])
        return " ".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    except Exception:  # noqa: BLE001
        return ""


def _lookup_title(settings: Settings, title: str, category: Optional[str]) -> str:
    """Best-effort source_url for a cited document title: one hybrid search on the index, scoped to the skill's category."""
    try:
        from . import search_index as SI

        hits = SI.hybrid_search(settings, title, category=category or None, k=1)
        return (hits[0].get("source_url") or "") if hits else ""
    except Exception:  # noqa: BLE001
        return ""


def _citations_from_markers(outputs: list[str], references: list, lookup_title=None) -> list[Citation]:
    """Citations named in the specialist's A2A output (title inside the marker), resolved to source URLs via the Sources
    retrieve when the titles match; otherwise a title-only citation."""
    seen: list[str] = []
    for out in outputs:
        for title in _CITE_MARKER_RE.findall(out):
            title = title.strip()
            if title and title not in seen:
                seen.append(title)
    by_title = {r.title.strip(): r.source_url for r in references if getattr(r, "title", "")}
    cites: list[Citation] = []
    for title in seen:
        url = by_title.get(title) or next((u for tt, u in by_title.items() if title in tt or tt in title), "")
        if not url and lookup_title is not None:
            url = lookup_title(title) or ""
        cites.append(Citation(title=title, url=url))
    return cites

