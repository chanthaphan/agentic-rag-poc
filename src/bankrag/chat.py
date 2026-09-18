"""Chat session: route to a skill, run its LangGraph agent on the session's thread, collect citations and sources."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator, Optional

import logging

import tiktoken
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from . import knowledge_base as KB
from . import kb_tools as KBT
from . import router as R
from . import rules as RL
from . import graph as G
from .checkpoints import delete_thread, has_thread, new_thread_id, saver
from .graph import REPLY_HINT, SupervisorContext, TurnCollector, TurnContext, location_note  # noqa: F401 - re-exported
from .llm import chat_model, sum_usage
from .pricing import load_pricing, turn_cost, usage_cost
from .config import Settings
from . import search_index as SI
from .sync import runtime_definition, spec_hash, synced_kb_owners, published_definition
from .models import CONCIERGE_AGENT, Answer, Citation, RouteDecision, SessionRecord, SkillSpec

OFFTOPIC_REPLY = {
    "th": "ขออภัยค่ะ ผู้ช่วยนี้ตอบได้เฉพาะคำถามเกี่ยวกับผลิตภัณฑ์ของธนาคารกรุงเทพ เช่น บัตรเครดิต บัตรเดบิต ประกัน และการลงทุน",
    "en": "Sorry, this assistant only answers questions about Bangkok Bank products such as credit cards, debit cards, insurance and investments.",
}
MIN_CONFIDENCE = 0.5
HISTORY_TURNS = 6  # default window of earlier question/answer pairs the agent sees (HISTORY_TURNS in settings)
MAX_TOOL_ROUNDS = 6
PREFETCH_DOCS = 8  # chunks fetched before the model runs (a chunk is ~450 tokens); top_k still bounds the Sources card
audit = logging.getLogger("bankrag.audit")
log = logging.getLogger("bankrag.chat")
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sources")
_HINT_ECHO_RE = re.compile(r"\s*\((?:โปรดตอบเป็นภาษาไทย|Please reply in English\.?)\)\s*")
# The coordinates ride along with the question (see question_with_location); if an agent ever echoes that line back,
# the customer must not read their own latitude out of a chat bubble.
_LOCATION_ECHO_RE = re.compile(r"\s*\[\s*customer location:[^\]]*\]\s*", re.I)


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
    """The question with the coordinates attached to it, for supervisor mode.

    A developer note reaches the concierge, but the specialist only ever sees the message the concierge chooses to
    send it - and a model asked to copy numbers across a handoff sometimes does not. Attached to the question itself,
    the coordinates travel with the one thing the concierge is told to relay verbatim.
    """
    if not location:
        return question
    lat, lon = location
    return (f"{question}\n[customer location: latitude {lat:.6f}, longitude {lon:.6f} - pass this line on to the "
            "specialist; it is not part of what the customer said and must never be shown to them]")


def detect_language(text: str) -> str:
    """'th' if the message contains Thai script (Thai with English product names still counts as Thai), else 'en'."""
    thai = sum(1 for ch in text if "฀" <= ch <= "๿")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if thai == 0:
        return "en"
    return "th" if thai >= 0.25 * max(1, thai + latin) else "en"


def suggestion_language(text: str) -> str:
    return detect_language(text)


LlmFactory = Callable[[str], Any]


class ChatSession:
    def __init__(self, settings: Settings, skills: dict[str, SkillSpec], *, llm_factory: Optional[LlmFactory] = None, checkpointer=None,
                 base_body: Optional[str] = None):
        self.settings = settings
        self.skills = skills
        self._llm_factory = llm_factory or (lambda model: chat_model(settings, model))
        self._llms: dict[str, Any] = {}
        self._checkpointer = checkpointer
        self._graphs: dict[tuple, Any] = {}
        self._base_body = base_body
        self.conversation_id: Optional[str] = None  # the LangGraph thread id
        self.last_location: Optional[tuple[float, float]] = None  # so a later turn can still put a pin on the map
        self.history: list[dict[str, str]] = []
        self.prev_skill: Optional[str] = None
        self.turns_in_conversation = 0
        self._seed_from: list[dict[str, str]] = []  # turns to seed a thread that has no checkpoint yet
        # the knowledge base each agent really got at the last sync, so Sources look where the agent retrieves from
        self.kb_owners = synced_kb_owners(settings, skills)

    # ---- building blocks ----
    def llm(self, model: str):
        if model not in self._llms:
            self._llms[model] = self._llm_factory(model)
        return self._llms[model]

    @property
    def checkpointer(self):
        if self._checkpointer is None:
            self._checkpointer = saver(self.settings)
        return self._checkpointer

    def base_body(self) -> str:
        if self._base_body is None:
            from .skills import load_base

            self._base_body = load_base(self.settings.skills_dir)
        return self._base_body

    def _definition_for(self, spec: SkillSpec):
        return runtime_definition(self.settings, spec, self.base_body(), self.kb_owners)

    def _graph_for(self, spec: SkillSpec, *, stateless: bool = False):
        """(compiled graph, definition, version) for a skill; graphs are cached by definition hash."""
        from .tools import tools_for

        definition, version = self._definition_for(spec)
        key = (spec.id, spec_hash(definition), stateless)
        if key not in self._graphs:
            tools = tools_for(self.settings, definition)
            self._graphs[key] = G.build_skill_graph(self.llm(definition.model), tools, checkpointer=None if stateless else self.checkpointer)
        return self._graphs[key], definition, version

    def _turn_context(self, definition, lang: str, location) -> TurnContext:
        return TurnContext(instructions=definition.instructions, language=lang, location=location,
                           history_turns=max(0, int(getattr(self.settings, "history_turns", HISTORY_TURNS))), max_tool_rounds=MAX_TOOL_ROUNDS)

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.conversation_id}, "recursion_limit": 2 * MAX_TOOL_ROUNDS + 6}

    def _ensure_thread(self, graph) -> bool:
        """A thread for this session; seeds it from the stored turns when it has no memory yet. Returns True when new."""
        if self.conversation_id and (not self.conversation_id.startswith("thr_") or not has_thread(self.checkpointer, self.conversation_id)) and self._seed_from:
            # a record from before the LangGraph memory (a Foundry conversation id) or a restored backup: rebuild the thread
            self.conversation_id = new_thread_id() if not self.conversation_id.startswith("thr_") else self.conversation_id
            msgs = [HumanMessage(content=t["content"]) if t["role"] == "user" else AIMessage(content=t["content"]) for t in self._seed_from if t.get("content")]
            try:
                graph.update_state(self._config(), {"messages": msgs})
            except Exception as e:  # noqa: BLE001 - memory is a convenience; the turn still works without it
                log.warning("could not seed thread %s: %s: %s", self.conversation_id, type(e).__name__, e)
            self._seed_from = []
            self.turns_in_conversation = 0
            return True
        if not self.conversation_id or not self.conversation_id.startswith("thr_"):
            self.conversation_id = new_thread_id()
            self.turns_in_conversation = 0
            self._seed_from = []
            return True
        return False

    # ---- routing policy ----
    def decide(self, question: str, force_skill: Optional[str] = None) -> RouteDecision:
        if force_skill:
            if force_skill not in self.skills:
                raise ValueError(f"unknown skill '{force_skill}'")
            return RouteDecision(skill_id=force_skill, confidence=1.0, reason="forced by caller", language=detect_language(question))
        pub = published_definition(self.settings, R.ROUTER_AGENT)
        d = R.route(self.llm(self.settings.router_model), question, self.history, self.prev_skill, self.skills,
                    instructions=pub[0].instructions if pub else None)
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
        """Yields events: route -> conversation -> status -> delta* -> tool* -> done(answer). The Sources retrieve runs in parallel."""
        # The browser asks for a position only when the question looks like a place question, and the customer can
        # refuse or the prompt can time out. Where they were a moment ago is still where they are, so keep it for the
        # map pin - it is never sent to an agent, only used to look a branch back up.
        if location:
            self.last_location = location
        map_location = location or self.last_location
        t_start = time.perf_counter()
        if self.settings.orchestration_mode == "supervisor" and not force_skill:
            yield from self._ask_supervisor(question, t_start, with_sources=with_sources, location=location)
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
        graph, definition, version = self._graph_for(spec)
        new_thread = self._ensure_thread(graph)
        yield {"type": "conversation", "conversation_id": self.conversation_id, "rotated": False, "new": new_thread}

        owner = self.kb_owners.get(spec.id, spec)
        future = self._sources_future(spec, owner, question) if with_sources else None
        yield {"type": "status", "phase": "retrieving" if future is not None else "drafting", "skill_id": spec.id}

        # The documents are fetched once, before the model runs, and handed to it together with the question: in the
        # common case one model call writes the answer, and the same documents fill the Sources card. The tool stays
        # bound for the follow-ups the prefetch does not cover. (Before, the same retrieve ran in parallel only to
        # feed the card, while the model made a second round trip to fetch the documents again through its tool.)
        references: list = []
        context = ""
        sources_ms = 0
        prefetch_error = ""
        if future is not None:
            t_src = time.perf_counter()
            try:
                references = future.result(timeout=60)
                context = KBT.format_context(references)
            except Exception as e:  # noqa: BLE001 - the model can still retrieve through its tool
                prefetch_error = f"{type(e).__name__}: {str(e)[:200]}"
            sources_ms = int((time.perf_counter() - t_src) * 1000)  # the prefetch is on the critical path now
            yield {"type": "status", "phase": "drafting", "skill_id": spec.id}

        t_agent = time.perf_counter()
        collector = TurnCollector(spec.id)
        ctx = self._turn_context(definition, lang, location)
        ctx.context = context
        for item in graph.stream({"messages": [HumanMessage(content=question)]}, config=self._config(),
                                 context=ctx, stream_mode=["messages", "updates", "custom"]):
            yield from collector.feed(item)
        agent_ms = int((time.perf_counter() - t_agent) * 1000)
        raw_text = collector.final_text
        tool_calls = list(collector.tool_calls)
        if context:  # the prefetch is a retrieval like any other: the trace, the evals and the card all see it
            tool_calls.insert(0, {"type": "mcp_call", "name": KBT.KB_TOOL, "arguments": json.dumps({"query": question}, ensure_ascii=False)[:500],
                                  "output": context[:1500], "error": "", "prefetched": True})
        elif prefetch_error:
            tool_calls.append({"type": "sources_error", "error": prefetch_error})
        agent_usage = collector.usage()
        retrieval = collector.retrieval(_tokens)
        if context:
            retrieval["calls"] += 1
            retrieval["documents"] += len(references)
            retrieval["output_chars"] += len(context)
            retrieval["output_tokens"] += _tokens(context)
            retrieval["query_variants"] = [question] + list(retrieval["query_variants"])
            retrieval["prefetched"] = True
        trace = {
            "timings_ms": {"route": decision.elapsed_ms, "agent": agent_ms, "sources": sources_ms, "total": int((time.perf_counter() - t_start) * 1000)},
            "usage": {"router": decision.usage, "agent": agent_usage, "total": sum_usage(decision.usage, agent_usage)},
            "retrieval": retrieval,
            "reasoning": [],
            "model": collector.model() or definition.model,
            "response_id": collector.response_id(),
            "agent_version": version,
        }
        try:
            trace["cost"] = turn_cost(load_pricing(self.settings), self.settings.router_model, trace["model"] or definition.model, trace["usage"], retrieval["calls"])
        except Exception as e:  # noqa: BLE001 - pricing must never break the chat
            trace["cost"] = {"error": str(e)[:120]}
        citations = self._citations(raw_text, collector.kb_outputs(), references, spec)
        text, appended = self._apply_rules(strip_markers(raw_text), question, lang, spec.id, trace)
        if appended:
            yield {"type": "delta", "text": appended}
        self.history.append({"role": "assistant", "content": text})
        self.prev_skill = spec.id
        self.turns_in_conversation += 1
        trace["conversation"] = {"id": self.conversation_id, "turn": self.turns_in_conversation, "rotated": False, "trimmed": collector.trimmed}
        asked = [t["content"] for t in self.history if t["role"] == "user"]
        yield {"type": "done", "answer": Answer(
            skill_id=spec.id,
            confidence=decision.confidence,
            route_reason=decision.reason,
            text=text,
            language=lang,
            suggestions=suggestions_for(self.settings, self._suggestions_llm(), spec, asked, self.skills,
                                        question=question, answer=text, language=lang, trace=trace),
            citations=citations,
            references=references,
            agent_name=spec.agent_name,
            agent_version=version,
            tool_calls=tool_calls,
            places=places_for_map(self.settings, text, map_location, question),
            conversation_id=self.conversation_id or "",
            trace=trace,
            retrieval_context=([context] if context else []) + collector.retrieval_context(),
        )}

    def _suggestions_llm(self):
        return self.llm(self.settings.suggestions_model or self.settings.router_model)

    def _citations(self, raw_text: str, kb_outputs: list[str], references: list, spec: Optional[SkillSpec]) -> list[Citation]:
        """Citations of an answer: the 【n:m†title】 markers the model echoed (resolved through the Sources retrieve, the
        references inside the tool output, or one index lookup) plus the inline [title](url) links, then the search-service
        URLs swapped for real source URLs."""
        refs = list(references)
        for r in G.references_in_outputs(kb_outputs):
            # a reference without its document URL still carries the search-service URI, which resolve_citations repairs
            url = str(r.get("source_url") or r.get("uri") or "")
            title = str(r.get("title") or "")
            if url or title:
                refs.append(KB.Reference(id=str(r.get("id") or ""), title=title, source_url=url))
        category = spec.product_category if spec else None
        cites = citations_from_markers([raw_text], refs, lookup_title=lambda title: _lookup_title(self.settings, title, category))
        seen = {c.url for c in cites if c.url}
        for c in citations_from_text(raw_text):
            if c.url not in seen:
                seen.add(c.url)
                cites.append(c)
        return resolve_citations(cites, refs, lookup=lambda ids: _lookup_docs(self.settings, ids))

    def _sources_future(self, spec, owner, question: str):
        """Start the Sources lookup in the background: the skill's own knowledge base when it has one, otherwise a
        hybrid index search filtered to the skill's knowledge space (skills on the shared base, e.g. on the free tier)."""
        n = max(spec.top_k, PREFETCH_DOCS)
        if owner.id == spec.id:
            return _POOL.submit(KB.retrieve, self.settings, owner.kb_name, question, ks_name=owner.ks_name, max_docs=n)
        if spec.product_category in ("all", "*", ""):
            return None
        return _POOL.submit(_index_references, self.settings, question, spec.product_category, n)

    # ---- supervisor mode ----
    def _supervisor_graph(self):
        from .supervisor import concierge_definition, handoff_tool

        pub = published_definition(self.settings, CONCIERGE_AGENT)
        definition, version = pub if pub else (concierge_definition(self.settings, self.skills), "")
        key = ("__concierge__", spec_hash(definition), False)
        if key not in self._graphs:
            tools = [handoff_tool(self.skills[t["skill_id"]]) for t in definition.tools if t.get("type") == "handoff" and t.get("skill_id") in self.skills]
            self._graphs[key] = G.build_supervisor_graph(self.llm(definition.model), tools, checkpointer=self.checkpointer)
        return self._graphs[key], definition, version

    def _ask_supervisor(self, question: str, t_start: float, *, with_sources: bool = True,
                        location: Optional[tuple[float, float]] = None) -> Iterator[dict[str, Any]]:
        """Handoff mode: the concierge model picks a specialist skill; the specialist answers the customer directly."""
        from .supervisor import skill_id_of

        map_location = location or self.last_location
        lang = detect_language(question)
        self.history.append({"role": "user", "content": question})
        yield {"type": "route", "skill_id": "concierge", "confidence": 1.0, "language": lang, "reason": "handoff: the concierge chooses the specialist in-process", "agent_name": CONCIERGE_AGENT, "route_ms": 0}
        graph, definition, version = self._supervisor_graph()
        new_thread = self._ensure_thread(graph)
        yield {"type": "conversation", "conversation_id": self.conversation_id, "rotated": False, "new": new_thread}
        yield {"type": "status", "phase": "choosing", "skill_id": ""}

        specialists: dict[str, Any] = {}
        definitions: dict[str, Any] = {}

        def specialist_context(sid: str) -> TurnContext:
            d = definitions[sid]
            return self._turn_context(d, lang, location)

        for sid, spec in self.skills.items():
            try:
                g, d, _v = self._graph_for(spec, stateless=True)
            except Exception as e:  # noqa: BLE001 - one broken skill must not take the concierge down
                log.warning("specialist %s unavailable: %s: %s", sid, type(e).__name__, e)
                continue
            specialists[sid], definitions[sid] = g, d
        ctx = SupervisorContext(instructions=definition.instructions, language=lang, location=location,
                                history_turns=max(0, int(getattr(self.settings, "history_turns", HISTORY_TURNS))), max_tool_rounds=MAX_TOOL_ROUNDS,
                                specialists=specialists, specialist_context=specialist_context, skill_of_tool=lambda name: skill_id_of(name, self.skills))
        t_agent = time.perf_counter()
        collector = TurnCollector("concierge", supervisor=True)
        future = None  # Sources retrieve for the specialist's knowledge base, started as soon as the concierge picks one
        for item in graph.stream({"messages": [HumanMessage(content=question_with_location(question, location))]}, config=self._config(), context=ctx,
                                 stream_mode=["messages", "updates", "custom"], subgraphs=True):
            for ev in collector.feed(item):
                yield ev
                if ev["type"] == "status" and ev.get("phase") == "specialist" and future is None and with_sources and collector.specialist_id in self.skills:
                    target = self.skills[collector.specialist_id]
                    future = self._sources_future(target, self.kb_owners.get(target.id, target), question)
        agent_ms = int((time.perf_counter() - t_agent) * 1000)
        raw_text = collector.final_text
        tool_calls = list(collector.handoff_calls) + list(collector.tool_calls)
        specialist = collector.specialist_id if collector.specialist_id in self.skills else "concierge"
        spec = self.skills.get(specialist)
        references: list = []
        sources_ms = 0
        if future is not None:
            t_wait = time.perf_counter()
            try:
                references = future.result(timeout=60)
            except Exception as e:  # noqa: BLE001
                tool_calls.append({"type": "sources_error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
            sources_ms = int((time.perf_counter() - t_wait) * 1000)
        concierge_usage, specialist_usage = collector.usage(), collector.specialist_usage()
        retrieval = collector.retrieval(_tokens)
        trace = {
            "timings_ms": {"route": 0, "agent": agent_ms, "sources": sources_ms, "total": int((time.perf_counter() - t_start) * 1000)},
            "usage": {"router": {}, "agent": concierge_usage, "specialist": specialist_usage, "total": sum_usage(concierge_usage, specialist_usage)},
            "retrieval": retrieval, "reasoning": [], "model": collector.model() or definition.model, "response_id": collector.response_id(),
            "agent_version": version,
            "handoff": {"mode": "supervisor", "concierge": CONCIERGE_AGENT, "specialist": specialist, "calls": list(collector.handoff_calls), "usage_pending": False},
        }
        try:
            pricing = load_pricing(self.settings)
            trace["cost"] = turn_cost(pricing, self.settings.router_model, definition.model, {"router": {}, "agent": concierge_usage, "total": concierge_usage}, 0)
            if spec is not None and specialist_usage.get("total_tokens"):
                sp_model = (definitions.get(specialist).model if definitions.get(specialist) else None) or self.settings.default_chat_model
                sp_cost = usage_cost(pricing, sp_model, specialist_usage)
                trace["cost"]["specialist"] = sp_cost
                trace["cost"]["total_usd"] = round(float(trace["cost"].get("total_usd", 0.0)) + float(sp_cost.get("total_usd", 0.0)), 6)
            trace["cost"]["note"] = "concierge and specialist tokens, both measured in-process"
        except Exception as e:  # noqa: BLE001
            trace["cost"] = {"error": str(e)[:120]}
        citations = self._citations(raw_text, collector.kb_outputs(), references, spec)
        text, appended = self._apply_rules(strip_markers(raw_text), question, lang, specialist, trace)
        if appended:
            yield {"type": "delta", "text": appended}
        self.history.append({"role": "assistant", "content": text})
        self.prev_skill = specialist if specialist in self.skills else self.prev_skill
        self.turns_in_conversation += 1
        trace["conversation"] = {"id": self.conversation_id, "turn": self.turns_in_conversation, "rotated": False, "trimmed": collector.trimmed}
        asked = [h["content"] for h in self.history if h["role"] == "user"]
        yield {"type": "done", "answer": Answer(
            skill_id=specialist, confidence=1.0,
            route_reason=f"concierge handed off to {spec.agent_name}" if spec else "concierge answered without a handoff",
            text=text, language=lang,
            suggestions=suggestions_for(self.settings, self._suggestions_llm(), spec or self.skills.get("general"), asked, self.skills,
                                        question=question, answer=text, language=lang, trace=trace),
            citations=citations, references=references, agent_name=CONCIERGE_AGENT, agent_version=version, tool_calls=tool_calls,
            places=places_for_map(self.settings, text, map_location, question), conversation_id=self.conversation_id or "", trace=trace,
            retrieval_context=collector.retrieval_context(),
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

    # ---- persistence ----
    @classmethod
    def from_record(cls, settings: Settings, skills: dict[str, SkillSpec], rec: SessionRecord, **kw) -> "ChatSession":
        s = cls(settings, skills, **kw)
        s.conversation_id = rec.conversation_id
        s.prev_skill = rec.prev_skill if rec.prev_skill in skills else None
        s.history = [{"role": t.role, "content": t.text} for t in rec.turns]
        s._seed_from = list(s.history)  # used only if the thread has no memory yet (see _ensure_thread)
        s.turns_in_conversation = sum(1 for t in rec.turns if t.role == "assistant" and (t.trace or {}).get("conversation", {}).get("id") == rec.conversation_id)
        return s

    def to_record(self, rec: SessionRecord) -> SessionRecord:
        rec.conversation_id = self.conversation_id
        rec.prev_skill = self.prev_skill
        return rec

    def reset(self) -> None:
        if self.conversation_id:
            try:
                delete_thread(self.checkpointer, self.conversation_id)
            except Exception:  # noqa: BLE001
                pass
        self.conversation_id, self.history, self.prev_skill, self._seed_from = None, [], None, []
        self.turns_in_conversation = 0


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
    """Remove citation markers like 【4:0†source】, any echoed reply-language hint, and source narration from the visible answer."""
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


def dynamic_suggestions(llm, question: str, answer: str, language: str, model: str = "",
                        asked: Optional[list[str]] = None) -> list[str]:
    """Follow-ups written from the turn that just happened, so they track the conversation instead of the catalogue.

    Returns [] on any failure or timeout: the caller falls back to the skill's static list, because a missing chip row
    is a much smaller problem than a slow or broken answer."""
    lang_name = "Thai" if language == "th" else "English"
    already = "\n".join(f"- {a}" for a in (asked or [])[-5:])
    try:
        resp = llm.invoke([
            SystemMessage(content=SUGGEST_PROMPT.format(lang_name=lang_name)),
            HumanMessage(content=f"Customer asked: {question}\n\nAssistant answered:\n{answer[:2500]}"
                                 + (f"\n\nAlready asked earlier (do not repeat):\n{already}" if already else "")),
        ])
        raw = G._text(resp).strip() if hasattr(resp, "content") else str(resp or "").strip()
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


def suggestions_for(settings: Settings, llm, spec, asked: list[str], skills: dict, *, question: str,
                    answer: str, language: str, trace: dict[str, Any]) -> list[str]:
    """Dynamic follow-ups when SUGGESTIONS_MODE allows it, with the skill's static list as the fallback."""
    static = pick_suggestions(spec, asked, skills, language=language) if spec else []
    if settings.suggestions_mode != "dynamic" or not answer.strip():
        trace["suggestions"] = {"mode": "static"}
        return static
    t0 = time.perf_counter()
    model = settings.suggestions_model or settings.router_model
    dyn = dynamic_suggestions(llm, question, answer, language, model, asked)
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


_CITE_MARKER_RE = re.compile(r"【\d+:\d+†([^】]+)】")
_LINK_RE = re.compile(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)")


def _lookup_title(settings: Settings, title: str, category: Optional[str]) -> str:
    """Best-effort source_url for a cited document title: one hybrid search on the index, scoped to the skill's category."""
    try:
        from . import search_index as SI

        hits = SI.hybrid_search(settings, title, category=category or None, k=1)
        return (hits[0].get("source_url") or "") if hits else ""
    except Exception:  # noqa: BLE001
        return ""


def citations_from_text(text: str) -> list[Citation]:
    """The inline [title](url) links an answer carries (the base prompt asks for citations in that form)."""
    out: list[Citation] = []
    seen: set[str] = set()
    for title, url in _LINK_RE.findall(text or ""):
        if url not in seen:
            seen.add(url)
            out.append(Citation(title=title.strip(), url=url))
    return out


def citations_from_markers(outputs: list[str], references: list, lookup_title=None) -> list[Citation]:
    """Citations named by 【n:m†title】 markers (in the answer or a tool output), resolved to source URLs via the
    references when the titles match; otherwise looked up by title, otherwise title-only."""
    seen: list[str] = []
    for out in outputs:
        for title in _CITE_MARKER_RE.findall(out or ""):
            title = title.strip()
            if title and title not in seen:
                seen.append(title)
    by_title = {r.title.strip(): r.source_url for r in references if getattr(r, "title", "") and getattr(r, "source_url", "")}
    cites: list[Citation] = []
    for title in seen:
        url = by_title.get(title) or next((u for tt, u in by_title.items() if title in tt or tt in title), "")
        if not url and lookup_title is not None:
            url = lookup_title(title) or ""
        cites.append(Citation(title=title, url=url))
    return cites


_citations_from_markers = citations_from_markers  # older name
