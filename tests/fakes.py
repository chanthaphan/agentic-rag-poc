"""Offline stand-ins for the chat model and the knowledge-base tool, so the graph tests never touch Azure."""
from __future__ import annotations

import json
from typing import Any, Callable, Iterator, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool, ToolException
from pydantic import Field


def ai(text: str = "", tool_calls: Optional[list[dict]] = None, usage: Optional[dict] = None, model: str = "gpt-4.1-mini", id: str = "") -> AIMessage:
    """A scripted reply: text, optional tool calls [{name, args, id}], optional usage (input/output tokens)."""
    calls = [{"name": c["name"], "args": c.get("args") or {}, "id": c.get("id") or f"call_{i}", "type": "tool_call"} for i, c in enumerate(tool_calls or [])]
    u = None
    if usage:
        u = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
             "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)}
    return AIMessage(content=text, tool_calls=calls, usage_metadata=u, response_metadata={"model_name": model, "id": id or "resp_1"}, id=id or None)


class FakeAgentModel(BaseChatModel):
    """Replays scripted AIMessages (or asks `responder`); streams them in pieces with tool calls and usage intact."""

    responses: list[AIMessage] = Field(default_factory=list)
    responder: Optional[Callable[[list[BaseMessage]], AIMessage]] = None
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    boom: bool = False
    pieces: int = 2
    tools_bound: bool = False  # set by bind_tools; without tools a scripted tool call cannot happen, so it is skipped

    @property
    def _llm_type(self) -> str:
        return "fake-agent"

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        if self.boom:
            raise RuntimeError("model down")
        if self.responder is not None:
            return self.responder(messages)
        while self.responses:
            msg = self.responses.pop(0)
            if msg.tool_calls and not self.tools_bound:
                continue  # no tools bound: the model has to answer in words, so skip to the next scripted answer
            return msg
        raise RuntimeError("fake model: no scripted reply left")

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None, run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages: list[BaseMessage], stop: Optional[list[str]] = None, run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        msg = self._next(messages)
        text = msg.content if isinstance(msg.content, str) else ""
        n = max(1, self.pieces)
        step = max(1, -(-len(text) // n)) if text else 1
        parts = [text[i:i + step] for i in range(0, len(text), step)] or [""]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            chunk = AIMessageChunk(
                content=part,
                tool_call_chunks=[{"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": k, "type": "tool_call_chunk"} for k, tc in enumerate(msg.tool_calls)] if last else [],
                usage_metadata=msg.usage_metadata if last else None,
                response_metadata=dict(msg.response_metadata) if last else {},
                id=msg.id,
            )
            gen = ChatGenerationChunk(message=chunk)
            if run_manager:
                run_manager.on_llm_new_token(part, chunk=gen)
            yield gen

    def bind_tools(self, tools: Any, **kwargs: Any):  # a shallow copy sharing the script and the call log
        return self.model_copy(update={"tools_bound": True})

    def with_structured_output(self, schema: Any, **kwargs: Any):
        def run(messages: Any) -> dict:
            msg = self._next(list(messages) if isinstance(messages, list) else [messages])
            try:
                parsed = json.loads(msg.content)
                err = None
            except Exception as e:  # noqa: BLE001
                parsed, err = None, e
            return {"raw": msg, "parsed": parsed, "parsing_error": err}

        return RunnableLambda(run)


KB_OUTPUT = ('Retrieved 2 documents\n'
             '【1:0†Bangkok Bank Visa Platinum】 ค่าธรรมเนียมรายปี 3,000 บาท\n'
             '【1:1†Infinite Card benefits】 เข้าเลานจ์ได้ 2 ครั้งต่อปี')


def fake_kb_tool(outputs: Optional[list[str]] = None, *, calls: Optional[list[dict]] = None, fail: bool = False) -> StructuredTool:
    """A knowledge_base_retrieve that replays scripted outputs and records its arguments."""
    from bankrag.kb_tools import KB_DESCRIPTION, KB_TOOL, RetrieveArgs

    outs = list(outputs if outputs is not None else [KB_OUTPUT])
    seen = calls if calls is not None else []

    def retrieve(query: str, query_variants: Optional[list[str]] = None) -> str:
        seen.append({"query": query, "query_variants": query_variants})
        if fail:
            raise ToolException("knowledge base unavailable: HTTP 401")
        return outs.pop(0) if len(outs) > 1 else outs[0]

    return StructuredTool.from_function(retrieve, name=KB_TOOL, description=KB_DESCRIPTION, args_schema=RetrieveArgs, handle_tool_error=True)
