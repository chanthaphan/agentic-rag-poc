"""The skill's Foundry IQ knowledge base as a LangChain tool, reached over its MCP endpoint in Azure AI Search.

The agent calls `knowledge_base_retrieve` exactly as the Foundry agent did; the difference is that the app now
authenticates itself: a bearer token of the signed-in identity (KB_MCP_AUTH=identity, needs Search Index Data Reader)
or the query key (KB_MCP_AUTH=apikey). One MCP session per call: the endpoint is stateless and the graph runs
synchronously (FastAPI threadpool / CLI), so each call opens its own event loop.
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
KB_DESCRIPTION = ("Search the bank's product documents for this product family. Call it for every product question, with the "
                  "customer's question as the query (same language) and, when helpful, a few reworded variants.")


class RetrieveArgs(BaseModel):
    query: str = Field(description="The customer's question, or the fact to look up, in the customer's language.")
    query_variants: Optional[list[str]] = Field(default=None, description="Two or three rewordings or sub-questions to search as well (Thai and English); the search runs each of them.")


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


def kb_tool(settings: Settings, kb_name: str, *, server_url: str = "", auth: str = "", timeout: float = 60) -> StructuredTool:
    url = server_url or settings.kb_mcp_url(kb_name)

    def retrieve(query: str, query_variants: Optional[list[str]] = None) -> str:
        # the endpoint requires query_variants as a JSON array; without any from the model, the question itself is the one variant
        variants = [str(v) for v in (query_variants or []) if str(v).strip()] or [query]
        args: dict[str, Any] = {"query": query, "query_variants": variants}
        try:
            out = call_sync(url, kb_headers(settings, auth), KB_TOOL, args, timeout)
        except ToolException:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("knowledge_base_retrieve(%s) failed: %s: %s", kb_name, type(e).__name__, str(e)[:200])
            raise ToolException(f"knowledge base unavailable: {type(e).__name__}: {str(e)[:200]}") from e
        log.info("knowledge_base_retrieve(%s) query=%r -> %d chars", kb_name, query[:80], len(out))
        return out

    return StructuredTool.from_function(retrieve, name=KB_TOOL, description=KB_DESCRIPTION, args_schema=RetrieveArgs, handle_tool_error=True)
