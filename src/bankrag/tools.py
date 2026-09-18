"""The bank's live services as LangChain tools, called in-process by the skill agents.

The same bodies back `/mcp/services` (mcp_server.py), so an external MCP client and the in-process agent get the
identical answer. The knowledge-base tool lives in kb_tools.py; `tools_for` assembles the list a definition asks for."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from langchain_core.tools import BaseTool, StructuredTool

from . import services as SV
from .config import Settings
from .kb_tools import kb_tool
from .models import KB_PREFIX, KS_PREFIX, AgentDefinition

log = logging.getLogger("bankrag.audit")
TOOL_FX = "fx_rate"
TOOL_BRANCH = "find_branch"
SERVICE_TOOL_NAMES = (TOOL_FX, TOOL_BRANCH)

FX_DESCRIPTION = ("Today's Bangkok Bank foreign-exchange rate for one currency. Returns the buying and selling rate "
                  "and the time the bank last updated them. Use for any 'what is the rate for X' question. "
                  "Always show the customer the as_of time and that the rate can change during the day.")
BRANCH_DESCRIPTION = ("Bangkok Bank branches, ATMs, exchange booths, Wealth Lounges and business centres near a place, "
                      "closest first. Use it for any 'where is / which is nearest / does that branch open on Saturday' "
                      "question. For 'where can I exchange money' use kind='exchange' first, which finds real FX "
                      "booths. Pass the customer's coordinates when they have shared their location; otherwise pass "
                      "the province they named and the search runs from there. When the customer names a branch, pass "
                      "that as name and it is found wherever it is in the province, however far away. Never answer a "
                      "branch's address, phone, hours or services without calling this.")


def fx_rate_impl(settings: Settings, currency: str) -> dict[str, Any]:
    """currency: ISO code such as USD, EUR, JPY, or the name the customer used ('เยน', 'yen')."""
    t0 = time.perf_counter()
    try:
        out = SV.fx_rate(settings, currency)
        log.info("tool fx_rate(currency=%r) -> found=%s in %d ms", currency, out.get("found"), int((time.perf_counter() - t0) * 1000))
        return out
    except SV.ServiceError as e:
        log.warning("tool fx_rate(currency=%r) FAILED: %s", currency, e)
        return {"found": False, "currency": currency.upper(), "error": str(e),
                "say": "The live rate is not available right now; suggest the bank's website or staff."}


def find_branch_impl(settings: Settings, lat: Optional[float] = None, lon: Optional[float] = None, province: str = "",
                     kind: str = "branch", limit: int = 5, name: str = "") -> dict[str, Any]:
    """lat/lon: the customer's position, when they shared it. province: the Thai or English province name they said ('กรุงเทพ', 'Chiang Mai') - enough on its own, no coordinates needed. name: a branch the customer named ('ซีคอนสแควร์', 'สีลม') - returns that branch rather than the nearest ones. kind: branch | atm | atm plus | exchange (FX booth) | fcd | wealth lounge (Wealth Center) | business center; anything else is searched as a branch."""
    t0 = time.perf_counter()
    try:
        out = SV.find_branch(settings, lat, lon, province=province, kind=kind, limit=limit, name=name)
        log.info("tool find_branch(kind=%r -> %s, province=%r, name=%r, coords=%s) -> found=%s, %d place(s) in %d ms",
                 kind, out.get("kind", ""), province, name, "yes" if lat is not None and lon is not None else "no",
                 out.get("found"), len(out.get("branches") or []), int((time.perf_counter() - t0) * 1000))
        return out
    except SV.ServiceError as e:
        log.warning("tool find_branch(kind=%r) FAILED: %s", kind, e)
        return {"found": False, "error": str(e), "say": "The branch lookup is not available right now; suggest the bank's Locate Us page."}


def service_tools(settings: Settings) -> dict[str, BaseTool]:
    def fx_rate(currency: str) -> dict[str, Any]:
        """currency: ISO code such as USD, EUR, JPY, or the name the customer used ('เยน', 'yen')."""
        return fx_rate_impl(settings, currency)

    def find_branch(lat: Optional[float] = None, lon: Optional[float] = None, province: str = "", kind: str = "branch",
                    limit: int = 5, name: str = "") -> dict[str, Any]:
        """lat/lon: the customer's position, when they shared it. province: the Thai or English province name they said ('กรุงเทพ', 'Chiang Mai') - enough on its own, no coordinates needed. name: a branch the customer named ('ซีคอนสแควร์', 'สีลม') - returns that branch rather than the nearest ones. kind: branch | atm | atm plus | exchange (FX booth) | fcd | wealth lounge (Wealth Center) | business center; anything else is searched as a branch."""
        return find_branch_impl(settings, lat, lon, province=province, kind=kind, limit=limit, name=name)

    return {
        TOOL_FX: StructuredTool.from_function(fx_rate, name=TOOL_FX, description=FX_DESCRIPTION),
        TOOL_BRANCH: StructuredTool.from_function(find_branch, name=TOOL_BRANCH, description=BRANCH_DESCRIPTION),
    }


def tools_for(settings: Settings, definition: AgentDefinition, *, kb_tool_factory=None) -> list[BaseTool]:
    """One LangChain tool per tool ref of a definition (handoff refs are the supervisor's business, skipped here)."""
    factory = kb_tool_factory or kb_tool
    live = None
    out: list[BaseTool] = []
    for ref in definition.tools:
        kind = ref.get("type")
        if kind == "mcp":
            kb_name = ref.get("kb_name") or ""
            # a definition published before the REST transport names no knowledge source: it is the base's twin (kb-x -> ks-x)
            ks_name = ref.get("ks_name") or (KS_PREFIX + kb_name[len(KB_PREFIX):] if kb_name.startswith(KB_PREFIX) else "")
            out.append(factory(settings, kb_name, server_url=ref.get("server_url") or "", auth=ref.get("auth") or "",
                               ks_name=ks_name, top_k=ref.get("top_k"), transport=ref.get("transport") or ""))
        elif kind == "function":
            live = live if live is not None else service_tools(settings)
            t = live.get(str(ref.get("name")))
            if t is not None:
                out.append(t)
    return out
