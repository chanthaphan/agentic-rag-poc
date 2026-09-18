"""The LangGraph graphs behind a chat turn.

Router mode runs one graph per skill: `agent` (the skill's model with its tools) <-> `tools`, then `compact`, which
drops the turn's tool traffic from the checkpoint so the thread keeps only question/answer pairs. Supervisor mode runs
one graph per session: `concierge` picks a specialist with a handoff tool, `specialist` runs that skill's graph
(stateless) and its answer becomes the reply. `TurnCollector` turns the stream into the event protocol the API sends.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Iterator, Optional, TypedDict

from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, BaseMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from .llm import response_id, response_model, sum_usage, usage_from_message

KB_TOOL = "knowledge_base_retrieve"
NODE_AGENT, NODE_TOOLS, NODE_COMPACT, NODE_CONCIERGE, NODE_SPECIALIST = "agent", "tools", "compact", "concierge", "specialist"


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


@dataclass
class TurnContext:
    """Per-turn runtime context (never checkpointed)."""

    instructions: str
    language: str = "th"
    location: Optional[tuple[float, float]] = None
    history_turns: int = 6
    max_tool_rounds: int = 6
    context: str = ""  # documents already retrieved for this question (never checkpointed)


@dataclass
class SupervisorContext(TurnContext):
    """The concierge's context plus how to run each specialist."""

    specialists: dict[str, Any] = field(default_factory=dict)  # skill id -> compiled specialist graph
    specialist_context: Optional[Callable[[str], TurnContext]] = None
    skill_of_tool: Optional[Callable[[str], str]] = None  # handoff tool name -> skill id


CONTEXT_NOTE = ("Retrieved documents: the knowledge base has already been searched with the customer's question and the results "
                "follow, each with its source URL. Answer from them, and cite inline as [title](url) with those URLs. "
                "If the exact figure, condition or product the customer asked about is not in these documents, call "
                "knowledge_base_retrieve with two or three reworded queries (Thai and English) before answering; never answer "
                "approximately, and never send the customer elsewhere instead of searching.")
NO_CONTEXT_NOTE = ("No documents were retrieved in advance for this question. Call knowledge_base_retrieve (the customer's question "
                   "plus two or three reworded queries, Thai and English) before answering any product question.")

REPLY_HINT = {
    "th": "Reply-language note: the customer wrote in Thai. Write the entire answer in Thai (product names may stay in English). Do not mention this note.",
    "en": "Reply-language note: the customer wrote in English. Write the entire answer in English. Do not mention this note.",
}


def location_note(location: tuple[float, float]) -> str:
    """The customer's coordinates as a developer note, so a branch tool call can use them.

    The customer shared these deliberately for this question; they are not stored with the turn."""
    lat, lon = location
    return (f"Customer location note: the customer is at latitude {lat:.6f}, longitude {lon:.6f}. "
            "Use these coordinates when a tool needs a position (nearest branch, where to exchange money). "
            "Never read the coordinates out to the customer and never mention this note.")


def system_prompt(ctx: TurnContext) -> str:
    parts = [ctx.instructions.rstrip(), REPLY_HINT.get(ctx.language, REPLY_HINT["th"])]
    if ctx.location:
        parts.append(location_note(ctx.location))
    return "\n\n".join(parts)


# ---------------- history window ----------------
def split_turns(messages: list[AnyMessage]) -> tuple[list[list[AnyMessage]], list[AnyMessage]]:
    """(earlier turns as [Human, AI...] groups, the current turn from its HumanMessage on)."""
    last = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    if last < 0:
        return [], list(messages)
    history, cur = messages[:last], messages[last:]
    turns: list[list[AnyMessage]] = []
    for m in history:
        if isinstance(m, HumanMessage) or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return turns, cur


def _clean_history(turn: list[AnyMessage]) -> list[AnyMessage]:
    """Only the question and the final answer of an earlier turn (tool traffic never re-enters the prompt)."""
    out: list[AnyMessage] = []
    for m in turn:
        if isinstance(m, HumanMessage):
            out.append(m)
        elif isinstance(m, AIMessage) and not m.tool_calls and _text(m):
            out.append(AIMessage(content=_text(m)))
    return out


def recap_note(dropped: list[list[AnyMessage]]) -> str:
    """Seed the visible window with a short recap of the turns that fell out of it, so follow-ups keep working."""
    users = [_text(t[0]) for t in dropped if t and isinstance(t[0], HumanMessage)][-3:]
    last_answer = ""
    for t in reversed(dropped):
        ans = next((m for m in reversed(t) if isinstance(m, AIMessage) and not m.tool_calls and _text(m)), None)
        if ans is not None:
            last_answer = _text(ans)
            break
    recap = "Context from earlier in this chat (older turns were trimmed to save tokens). Earlier customer questions: " + " | ".join(users)
    if last_answer:
        recap += f"\nLast answer before the trim (abridged): {last_answer[:600]}"
    return recap


def prompt_messages(ctx: TurnContext, messages: list[AnyMessage], *, kb_tool: bool = False) -> tuple[list[BaseMessage], int]:
    """[system] + the last `history_turns` question/answer pairs (+ a recap of the dropped ones) + the retrieved documents
    (or, when the agent has a knowledge-base tool and nothing was prefetched, the order to search first) + the current turn."""
    turns, cur = split_turns(messages)
    keep = max(0, ctx.history_turns)
    dropped, kept = (turns[:-keep] if keep else turns), (turns[-keep:] if keep else [])
    out: list[BaseMessage] = [SystemMessage(content=system_prompt(ctx))]
    if dropped:
        out.append(SystemMessage(content=recap_note(dropped)))
    for t in kept:
        out.extend(_clean_history(t))
    if ctx.context:
        out.append(SystemMessage(content=CONTEXT_NOTE + "\n\n" + ctx.context))
    elif kb_tool:
        out.append(SystemMessage(content=NO_CONTEXT_NOTE))
    out.extend(cur)
    return out, len(dropped)


def compaction(messages: list[AnyMessage]) -> list[RemoveMessage]:
    """Remove this turn's tool-calling AI messages and tool results; the checkpoint keeps the question and the answer."""
    _, cur = split_turns(messages)
    out: list[RemoveMessage] = []
    for m in cur:
        if m.id and ((isinstance(m, AIMessage) and m.tool_calls) or isinstance(m, ToolMessage)):
            out.append(RemoveMessage(id=m.id))
    return out


def tool_rounds(messages: list[AnyMessage]) -> int:
    _, cur = split_turns(messages)
    return sum(1 for m in cur if isinstance(m, AIMessage) and m.tool_calls)


def _text(m: BaseMessage) -> str:
    c = m.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in c)
    return str(c or "")


# ---------------- skill graph (router mode; also each specialist) ----------------
def build_skill_graph(llm, tools: list[BaseTool], *, checkpointer=None):
    bound = llm.bind_tools(tools) if tools else llm
    has_kb = any(getattr(t, "name", "") == KB_TOOL for t in tools)

    def agent(state: AgentState, runtime: Runtime[TurnContext]) -> dict:
        ctx = runtime.context
        msgs, dropped = prompt_messages(ctx, state["messages"], kb_tool=has_kb)
        if dropped:
            try:
                get_stream_writer()({"trimmed": dropped})
            except Exception:  # noqa: BLE001 - no stream, nothing to report
                pass
        model = llm if tools and tool_rounds(state["messages"]) >= ctx.max_tool_rounds else bound  # cap reached: answer now
        ai = model.invoke(msgs)
        return {"messages": [ai]}

    def after_agent(state: AgentState, runtime: Runtime[TurnContext]) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls and tools and tool_rounds(state["messages"]) <= runtime.context.max_tool_rounds:
            return NODE_TOOLS
        return NODE_COMPACT

    def compact(state: AgentState) -> dict:
        return {"messages": compaction(state["messages"])}

    g = StateGraph(AgentState, context_schema=TurnContext)
    g.add_node(NODE_AGENT, agent)
    if tools:
        g.add_node(NODE_TOOLS, ToolNode(tools, handle_tool_errors=True))
        g.add_edge(NODE_TOOLS, NODE_AGENT)
    g.add_node(NODE_COMPACT, compact)
    g.add_edge(START, NODE_AGENT)
    g.add_conditional_edges(NODE_AGENT, after_agent, [NODE_TOOLS, NODE_COMPACT] if tools else [NODE_COMPACT])
    g.add_edge(NODE_COMPACT, END)
    return g.compile(checkpointer=checkpointer)


# ---------------- supervisor graph ----------------
def build_supervisor_graph(llm, handoff_tools: list[BaseTool], *, checkpointer=None):
    bound = llm.bind_tools(handoff_tools) if handoff_tools else llm

    def concierge(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
        msgs, dropped = prompt_messages(runtime.context, state["messages"])
        if dropped:
            try:
                get_stream_writer()({"trimmed": dropped})
            except Exception:  # noqa: BLE001
                pass
        return {"messages": [bound.invoke(msgs)]}

    def after_concierge(state: AgentState) -> str:
        last = state["messages"][-1]
        return NODE_SPECIALIST if isinstance(last, AIMessage) and last.tool_calls else NODE_COMPACT

    def specialist(state: AgentState, runtime: Runtime[SupervisorContext]) -> dict:
        ctx = runtime.context
        ai = state["messages"][-1]
        calls = list(ai.tool_calls)
        call = calls[0]
        sid = ctx.skill_of_tool(call["name"]) if ctx.skill_of_tool else ""
        args = call.get("args") or {}
        question = str(args.get("question") or "").strip()
        if not question:  # the model called the tool without repeating the question: use the customer's message
            human = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
            question = _text(human) if human is not None else ""
        if args.get("context"):
            question = f"{question}\n(Context from the conversation: {args['context']})"
        try:
            get_stream_writer()({"handoff": {"skill_id": sid, "tool": call["name"], "question": question}})
        except Exception:  # noqa: BLE001
            pass
        out: list[AnyMessage] = [ToolMessage(content=f"handed off to {sid or 'no specialist'}", tool_call_id=call["id"], name=call["name"])]
        for extra in calls[1:]:  # a model that asked for two specialists: only the first runs, the others are closed out
            out.append(ToolMessage(content="ignored: only one handoff per question", tool_call_id=extra["id"], name=extra["name"]))
        graph = ctx.specialists.get(sid)
        if graph is None or ctx.specialist_context is None:
            out.append(AIMessage(content=""))
            return {"messages": out}
        result = graph.invoke({"messages": [HumanMessage(content=question)]}, context=ctx.specialist_context(sid))
        final = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage) and not m.tool_calls), None)
        out.append(AIMessage(content=_text(final) if final is not None else ""))
        return {"messages": out}

    def compact(state: AgentState) -> dict:
        return {"messages": compaction(state["messages"])}

    g = StateGraph(AgentState, context_schema=SupervisorContext)
    g.add_node(NODE_CONCIERGE, concierge)
    g.add_node(NODE_SPECIALIST, specialist)
    g.add_node(NODE_COMPACT, compact)
    g.add_edge(START, NODE_CONCIERGE)
    g.add_conditional_edges(NODE_CONCIERGE, after_concierge, [NODE_SPECIALIST, NODE_COMPACT])
    g.add_edge(NODE_SPECIALIST, NODE_COMPACT)
    g.add_edge(NODE_COMPACT, END)
    return g.compile(checkpointer=checkpointer)


# ---------------- stream -> events ----------------
_RETRIEVED_RE = re.compile(r"Retrieved (\d+) documents")


class TurnCollector:
    """Consumes `graph.stream(..., stream_mode=["messages", "updates", "custom"])` and yields the chat event protocol.

    Router mode: deltas come from the `agent` node. Supervisor mode: from `concierge` (small talk) and from the
    specialist's `agent` node (namespaced when the parent streams with subgraphs=True); the specialist's own updates
    are booked separately so the trace shows its usage."""

    def __init__(self, skill_id: str, *, supervisor: bool = False, skill_of_tool: Optional[Callable[[str], str]] = None) -> None:
        self.skill_id = skill_id
        self.supervisor = supervisor
        self.skill_of_tool = skill_of_tool
        self.ai_messages: list[AIMessage] = []
        self.specialist_ai: list[AIMessage] = []
        self.tool_messages: list[ToolMessage] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.tool_args: dict[str, dict[str, Any]] = {}
        self.trimmed = 0
        self.specialist_id = ""
        self.handoff_calls: list[dict[str, Any]] = []
        self.text = ""
        self._streamed = ""
        self._retrieving = False
        self._delta_count = 0

    # -- feeding --
    def feed(self, item: Any) -> Iterator[dict[str, Any]]:
        ns: tuple = ()
        if isinstance(item, tuple) and len(item) == 3:
            ns, mode, payload = item
        elif isinstance(item, tuple) and len(item) == 2:
            mode, payload = item
        else:
            return
        ns = tuple(ns or ())
        if mode == "messages":
            yield from self._on_message(ns, payload)
        elif mode == "updates":
            yield from self._on_update(ns, payload)
        elif mode == "custom":
            yield from self._on_custom(ns, payload)

    def _on_custom(self, ns: tuple, payload: Any) -> Iterator[dict[str, Any]]:
        if not isinstance(payload, dict):
            return
        if "trimmed" in payload:
            self.trimmed = max(self.trimmed, int(payload["trimmed"] or 0))
        if "handoff" in payload:
            h = payload["handoff"] or {}
            self.specialist_id = str(h.get("skill_id") or "")
            self.handoff_calls.append({"type": "handoff", "name": str(h.get("tool") or ""), "skill_id": self.specialist_id, "question": str(h.get("question") or "")[:500]})
            yield {"type": "tool", "name": str(h.get("tool") or ""), "arguments": str(h.get("question") or "")[:300], "error": "" if self.specialist_id else "unknown specialist"}
            yield {"type": "status", "phase": "specialist", "skill_id": self.specialist_id}

    def _on_message(self, ns: tuple, payload: Any) -> Iterator[dict[str, Any]]:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        msg, meta = payload
        node = str((meta or {}).get("langgraph_node") or "")
        if not isinstance(msg, AIMessageChunk):
            return
        if node not in (NODE_AGENT, NODE_CONCIERGE):
            return
        if msg.tool_call_chunks and not self._retrieving and node == NODE_AGENT:
            self._retrieving = True
            yield {"type": "status", "phase": "retrieving", "skill_id": self.specialist_id or self.skill_id}
        t = _text(msg)
        if t:
            self._streamed += t
            self._delta_count += 1
            yield {"type": "delta", "text": t}

    def _on_update(self, ns: tuple, payload: Any) -> Iterator[dict[str, Any]]:
        if not isinstance(payload, dict):
            return
        for node, upd in payload.items():
            msgs = (upd or {}).get("messages") if isinstance(upd, dict) else None
            if not msgs:
                continue
            for m in msgs:
                if isinstance(m, RemoveMessage):
                    continue
                if isinstance(m, AIMessage):
                    if node == NODE_AGENT and ns:
                        self.specialist_ai.append(m)
                    elif node in (NODE_AGENT, NODE_CONCIERGE):
                        self.ai_messages.append(m)
                    for tc in m.tool_calls or []:
                        self.tool_args[str(tc.get("id"))] = dict(tc.get("args") or {})
                    if not m.tool_calls and _text(m):
                        self.text = _text(m)
                elif isinstance(m, ToolMessage):
                    if node == NODE_SPECIALIST:
                        continue  # the handoff closure, already reported from the custom event
                    self.tool_messages.append(m)
                    self._retrieving = False
                    args = self.tool_args.get(str(m.tool_call_id), {})
                    err = _text(m)[:300] if getattr(m, "status", "") == "error" else ""
                    self.tool_calls.append({
                        "type": "mcp_call" if m.name == KB_TOOL else "function_call", "name": m.name or "",
                        "arguments": json.dumps(args, ensure_ascii=False)[:500], "output": _text(m)[:1500], "error": err,
                    })
                    yield {"type": "tool", "name": m.name or "", "arguments": json.dumps(args, ensure_ascii=False)[:300], "error": err}
                    yield {"type": "status", "phase": "drafting", "skill_id": self.specialist_id or self.skill_id}

    # -- results --
    @property
    def final_text(self) -> str:
        return self.text or self._streamed

    def usage(self) -> dict[str, int]:
        return sum_usage(*(usage_from_message(m) for m in self.ai_messages)) if self.ai_messages else {}

    def specialist_usage(self) -> dict[str, int]:
        return sum_usage(*(usage_from_message(m) for m in self.specialist_ai)) if self.specialist_ai else {}

    def last_ai(self) -> Optional[AIMessage]:
        pool = self.specialist_ai or self.ai_messages
        return pool[-1] if pool else None

    def model(self) -> str:
        return response_model(self.last_ai())

    def response_id(self) -> str:
        return response_id(self.last_ai())

    def kb_outputs(self) -> list[str]:
        return [_text(m) for m in self.tool_messages if m.name == KB_TOOL and getattr(m, "status", "") != "error" and _text(m).strip()]

    def retrieval(self, count_tokens: Callable[[str], int]) -> dict[str, Any]:
        out = {"calls": 0, "documents": 0, "output_chars": 0, "output_tokens": 0, "query_variants": []}
        for m in self.tool_messages:
            if m.name != KB_TOOL:
                continue
            s = _text(m)
            out["calls"] += 1
            mm = _RETRIEVED_RE.search(s)
            out["documents"] += int(mm.group(1)) if mm else (len(_json_refs(s)) or s.count("【"))
            out["output_chars"] += len(s)
            out["output_tokens"] += count_tokens(s)
            args = self.tool_args.get(str(m.tool_call_id), {})
            out["query_variants"] += list(args.get("query_variants") or ([args["query"]] if args.get("query") else []))
        return out

    def retrieval_context(self) -> list[str]:
        return [s[:60000] for s in self.kb_outputs()]


def _json_docs(s: str) -> list[Any]:
    """Every JSON document in a tool output (the knowledge base returns several, one per content part)."""
    s = s.strip()
    if not s or s[0] not in "[{":
        return []
    dec = json.JSONDecoder()
    out: list[Any] = []
    i = 0
    try:
        while i < len(s):
            while i < len(s) and s[i].isspace():
                i += 1
            if i >= len(s):
                break
            obj, i = dec.raw_decode(s, i)
            out.append(obj)
    except Exception:  # noqa: BLE001 - keep what parsed
        pass
    return out


def _json_refs(s: str) -> list[dict[str, Any]]:
    """The documents inside a knowledge-base tool output as {id, ref_id, title, source_url, uri, content}.

    The MCP endpoint answers with a list of chunks ({ref_id, title, terms, content}) followed by one reference object
    per chunk ({kind: "reference", ref_id, uri, sourceData: {id, title, source_url, ...}}); older shapes with a
    `references` list are read too."""
    chunks: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def chunk(it: dict[str, Any]) -> None:
        rid = str(it.get("ref_id", len(chunks)))
        if rid not in chunks:
            order.append(rid)
        chunks[rid] = it

    for doc in _json_docs(s):
        if isinstance(doc, list):
            for it in doc:
                if isinstance(it, dict):
                    chunk(it)
        elif isinstance(doc, dict):
            if doc.get("kind") == "reference" or "uri" in doc or "sourceData" in doc or "source_data" in doc:
                refs[str(doc.get("ref_id", len(refs)))] = doc
            else:
                for key in ("references", "documents", "results", "value"):
                    if isinstance(doc.get(key), list):
                        for it in doc[key]:
                            if isinstance(it, dict):
                                chunk(it)
                        break
                else:
                    if "title" in doc or "content" in doc:
                        chunk(doc)
    out: list[dict[str, Any]] = []
    for rid in order or sorted(refs):
        c = chunks.get(rid, {})
        r = refs.get(rid, {})
        sd = r.get("sourceData") or r.get("source_data") or {}
        if isinstance(sd, str):
            try:
                sd = json.loads(sd)
            except Exception:  # noqa: BLE001
                m_url = re.search(r"['\"]source_url['\"]:\s*['\"]([^'\"]*)['\"]", sd)
                m_id = re.search(r"['\"]id['\"]:\s*['\"]([^'\"]*)['\"]", sd)
                sd = {"source_url": m_url.group(1) if m_url else "", "id": m_id.group(1) if m_id else ""}
        if not isinstance(sd, dict):
            sd = {}
        uri = str(r.get("uri") or c.get("uri") or "")
        out.append({
            "ref_id": rid,
            "id": str(sd.get("id") or c.get("id") or r.get("id") or (uri.split("/docs/", 1)[1].split("?", 1)[0] if "/docs/" in uri else "")),
            "title": str(c.get("title") or sd.get("title") or r.get("title") or ""),
            "source_url": str(sd.get("source_url") or c.get("source_url") or r.get("source_url") or c.get("url") or ""),
            "uri": uri,
            "content": str(c.get("content") or sd.get("content") or ""),
        })
    return out


def references_in_outputs(outputs: list[str]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for s in outputs:
        refs.extend(_json_refs(s))
    return refs
