"""The skill's Foundry IQ knowledge base as a LangChain tool, and the text block the model reads.

Two transports reach the same knowledge base:
- rest (default): one `retrieve` call through the search SDK, about a second from here; the model's query variants
  become extra search intents.
- mcp: the base's MCP endpoint, as the Foundry agent used it; a session is opened per call (a handshake of about
  1.4 s before the search starts), so it is kept for comparison rather than speed.

The app authenticates itself either way: the signed-in identity (KB_MCP_AUTH=identity, needs Search Index Data
Reader) or the query key (KB_MCP_AUTH=apikey) for MCP; the admin key for the REST retrieve, as the Sources panel
already does.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Optional

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from .azure_auth import SEARCH_SCOPE, token
from .config import Settings

log = logging.getLogger("bankrag.chat")
KB_TOOL = "knowledge_base_retrieve"
KB_DESCRIPTION = ("Search the bank's product documents for this product family. Use it when the documents already given to you "
                  "do not cover the question, with the customer's question as the query (same language) and a few reworded variants.")


class RetrieveArgs(BaseModel):
    query: str = Field(description="The customer's question, or the fact to look up, in the customer's language.")
    query_variants: Optional[list[str]] = Field(default=None, description="Two or three rewordings or sub-questions to search as well (Thai and English); the search runs each of them.")


# ---------------- the documents as the model reads them ----------------
def format_context(refs: list, *, max_chars: int = 0) -> str:
    """The retrieved documents as one block: a numbered title, the source URL, the chunk text.

    Starts with 'Retrieved N documents' so the retrieval statistics can count it like a tool output; returns ''
    when nothing was retrieved, so the caller can tell 'no documents' from 'no search'."""
    if not refs:
        return ""
    parts: list[str] = []
    for i, r in enumerate(refs, 1):
        head = f"[{i}] {getattr(r, 'title', '') or ''}".rstrip()
        url = getattr(r, "source_url", "") or ""
        if url:
            head += f"\n{url}"
        body = (getattr(r, "content", "") or getattr(r, "snippet", "") or "").strip()
        if max_chars and len(body) > max_chars:
            body = body[:max_chars] + " ..."
        parts.append(f"{head}\n{body}")
    return f"Retrieved {len(refs)} documents\n\n" + "\n\n---\n\n".join(parts)


def rest_retrieve(settings: Settings, kb_name: str, query: str, *, ks_name: str = "", max_docs: Optional[int] = None,
                  variants: Optional[list[str]] = None) -> tuple[str, list]:
    """(the text block for the model, the references for the Sources card) from one retrieve call."""
    from . import knowledge_base as KB

    refs = KB.retrieve(settings, kb_name, query, ks_name=ks_name or None, max_docs=max_docs, variants=variants)
    return format_context(refs), refs


# ---------------- the MCP transport ----------------
def kb_headers(settings: Settings, auth: str = "") -> dict[str, str]:
    mode = auth or settings.kb_mcp_auth
    if mode == "apikey":
        settings.require("search_query_key")
        return {"api-key": settings.search_query_key}
    return {"Authorization": f"Bearer {token(SEARCH_SCOPE)}"}


def _text_of(result: Any) -> str:
    """The text of an MCP tool result: text content parts joined, structured content as JSON when there is no text."""
    parts: list[str] = []
    for c in getattr(result, "content", None) or []:
        t = getattr(c, "text", None)
        if t:
            parts.append(str(t))
    if not parts:
        sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if sc:
            parts.append(json.dumps(sc, ensure_ascii=False))
    return "\n".join(parts)


async def _call(url: str, headers: dict[str, str], name: str, arguments: dict[str, Any], timeout: float = 60) -> str:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(timeout, read=timeout)) as http:
        async with Client(streamable_http_client(url, http_client=http), read_timeout_seconds=timeout) as client:
            result = await client.call_tool(name, arguments)
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise ToolException(_text_of(result) or "knowledge base returned an error")
    return _text_of(result)


def call_sync(url: str, headers: dict[str, str], name: str, arguments: dict[str, Any], timeout: float = 60) -> str:
    """Run the MCP round trip on its own loop; from inside a running loop, on a helper thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_call(url, headers, name, arguments, timeout))
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["out"] = asyncio.run(_call(url, headers, name, arguments, timeout))
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run, name="kb-mcp", daemon=True)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]
    return box["out"]


# ---------------- the tool ----------------
def kb_tool(settings: Settings, kb_name: str, *, server_url: str = "", auth: str = "", timeout: float = 60,
            ks_name: str = "", top_k: Optional[int] = None, transport: str = "") -> StructuredTool:
    url = server_url or settings.kb_mcp_url(kb_name)
    mode = (transport or settings.kb_transport or "rest").lower()

    def retrieve(query: str, query_variants: Optional[list[str]] = None) -> str:
        variants = [str(v) for v in (query_variants or []) if str(v).strip()]
        try:
            if mode == "mcp":
                # the endpoint requires query_variants as a JSON array; without any from the model, the question is the one variant
                out = call_sync(url, kb_headers(settings, auth), KB_TOOL, {"query": query, "query_variants": variants or [query]}, timeout)
            else:
                out, _refs = rest_retrieve(settings, kb_name, query, ks_name=ks_name, max_docs=top_k, variants=variants)
                out = out or "Retrieved 0 documents"
        except ToolException:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("knowledge_base_retrieve(%s, %s) failed: %s: %s", kb_name, mode, type(e).__name__, str(e)[:200])
            raise ToolException(f"knowledge base unavailable: {type(e).__name__}: {str(e)[:200]}") from e
        log.info("knowledge_base_retrieve(%s, %s) query=%r -> %d chars", kb_name, mode, query[:80], len(out))
        return out

    return StructuredTool.from_function(retrieve, name=KB_TOOL, description=KB_DESCRIPTION, args_schema=RetrieveArgs, handle_tool_error=True)
