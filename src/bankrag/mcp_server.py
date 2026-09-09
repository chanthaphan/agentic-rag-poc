"""An MCP server exposing the bank's live services as agent tools.

The knowledge base answers from documents; this answers from the bank's live APIs, which is what a rate or a branch
needs. Foundry agents reach it the same way they reach the knowledge base: an `MCPTool` pointed at a URL. The
difference is that this endpoint is ours, so it is mounted on the app (`/mcp/services`). In Azure it stays behind the
app's Easy Auth and the caller is pinned to the Foundry project's managed identity (`MCP_CALLER_PRINCIPALS`);
`MCP_TOOL_KEY` is the local-dev guard, where there is no Easy Auth to authenticate anyone.

The transport is stateless streamable HTTP with JSON responses: no session to keep, no SSE to hold open, so it can be
mounted straight into FastAPI and survives a container replica moving.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings
from . import services as SV

MCP_PATH = "/mcp/services"
AUTH_HEADER = "x-tool-key"
TOOL_FX = "fx_rate"
TOOL_BRANCH = "find_branch"


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

    @server.tool(
        name=TOOL_BRANCH,
        description="Bangkok Bank branches nearest to a pair of coordinates, closest first. Use when the customer asks "
                    "where a branch, an ATM or a currency-exchange booth is, or which is nearest. For 'where can I "
                    "exchange money' use kind='exchange' first, which finds real FX booths. The customer's "
                    "coordinates are given to you in the conversation when they have shared their location; if they "
                    "are not there, ask which province or district instead of guessing.",
    )
    def find_branch(lat: float, lon: float, province: str = "", kind: str = "branch", limit: int = 5) -> dict[str, Any]:
        """lat/lon: the customer's position. province: optional Thai province name. kind: 'branch', 'atm', 'atm plus', or 'exchange' for a currency-exchange booth."""
        try:
            return SV.find_branch(settings, lat, lon, province=province, kind=SV.resolve_kind(kind), limit=limit)
        except SV.ServiceError as e:
            return {"found": False, "error": str(e),
                    "say": "The branch lookup is not available right now; suggest the bank's Locate Us page."}

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
    async def app(scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            why = _reject(settings, headers)
            if why:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps({"error": why}).encode()})
                return
        await inner(scope, receive, send)

    return app, inner


def caller_object_id(headers: dict[str, str]) -> str:
    """The calling principal's object id, from the Easy Auth headers in front of the app."""
    oid = headers.get("x-ms-client-principal-id", "").strip()
    if oid:
        return oid
    raw = headers.get("x-ms-client-principal", "")
    if not raw:
        return ""
    try:
        claims = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")).get("claims") or []
    except Exception:  # noqa: BLE001
        return ""
    wanted = ("http://schemas.microsoft.com/identity/claims/objectidentifier", "oid")
    for c in claims:
        if str(c.get("typ", "")).lower() in wanted:
            return str(c.get("val", "")).strip()
    return ""


def _reject(settings: Settings, headers: dict[str, str]) -> str:
    """'' to allow, else why it was refused.

    In Azure the endpoint sits behind Easy Auth, so a caller has already proven a tenant identity; we additionally pin
    it to the Foundry project's managed identity, because Easy Auth alone would let any signed-in tenant user call the
    tool. MCP_TOOL_KEY stays as the local-dev guard, where there is no Easy Auth to authenticate anyone."""
    if settings.services_mcp_callers:
        oid = caller_object_id(headers)
        if not oid:
            return "no authenticated caller: this endpoint is reachable only through Easy Auth"
        if oid not in settings.services_mcp_callers:
            return "this identity is not allowed to call the bank-services tools"
        return ""
    if settings.services_mcp_key:
        return "" if headers.get(AUTH_HEADER) == settings.services_mcp_key else "bad or missing tool key"
    return "the bank-services endpoint is not configured for callers"
