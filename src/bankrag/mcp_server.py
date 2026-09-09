"""An MCP server exposing the bank's live services as agent tools.

The knowledge base answers from documents; this answers from the bank's live APIs, which is what a rate or a branch
needs. Foundry agents reach it the same way they reach the knowledge base: an `MCPTool` pointed at a URL. The
difference is that this endpoint is ours, so it is mounted on the app (`/mcp/services`) and guarded by a shared header
key (`MCP_TOOL_KEY`) - the same POC pattern as `KB_MCP_AUTH=apikey`.

The transport is stateless streamable HTTP with JSON responses: no session to keep, no SSE to hold open, so it can be
mounted straight into FastAPI and survives a container replica moving.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings
from . import services as SV

MCP_PATH = "/mcp/services"
AUTH_HEADER = "x-tool-key"
TOOL_FX = "fx_rate"


def build_server(settings: Settings) -> MCPServer:
    server = MCPServer(
        name="bank-services",
        instructions="Live Bangkok Bank services. Use these for anything that changes during the day; never answer "
                     "such a question from memory or from product documents.",
    )

    @server.tool(
        name=TOOL_FX,
        description="Today's Bangkok Bank foreign-exchange rate for one currency. Returns the buying and selling rate "
                    "and the time the bank last updated them. Use for any 'what is the rate for X' question. "
                    "Always show the customer the as_of time and that the rate can change during the day.",
    )
    def fx_rate(currency: str) -> dict[str, Any]:
        """currency: ISO code such as USD, EUR, JPY."""
        try:
            return SV.fx_rate(settings, currency)
        except SV.ServiceError as e:
            return {"found": False, "currency": currency.upper(), "error": str(e),
                    "say": "The live rate is not available right now; suggest the bank's website or staff."}

    return server


def build_asgi(settings: Settings) -> tuple[Callable, Any]:
    """(guarded ASGI app, the inner Starlette app).

    The caller must run the inner app's lifespan: even stateless streamable HTTP starts a task group in it, and
    Starlette's Mount does NOT run a mounted app's lifespan, so mounting alone gives
    "Task group is not initialized" on the first request.
    """
    # DNS-rebinding protection validates the Host header and defaults to localhost only, which would 421 every call
    # from Foundry to the container's public name. Allow exactly our own host (plus local dev / tests).
    from urllib.parse import urlparse

    hosts = ["localhost", "127.0.0.1", "testserver", "localhost:8010", "127.0.0.1:8010"]
    origins = []
    if settings.public_base_url:
        host = urlparse(settings.public_base_url).netloc
        if host:
            hosts.append(host)
            origins.append(settings.public_base_url)
    inner = build_server(settings).streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins),
    )
    expected = settings.services_mcp_key

    async def app(scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope["type"] == "http" and expected:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if headers.get(AUTH_HEADER) != expected:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"bad or missing tool key"}'})
                return
        await inner(scope, receive, send)

    return app, inner
