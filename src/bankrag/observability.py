"""Read Foundry server-side agent traces from Application Insights.

Foundry writes one span tree per response (invoke_agent -> chat / execute_tool ...) with OpenTelemetry GenAI attributes.
In A2A handoff mode the specialist's run happens inside Foundry and never reaches our process, so its token usage is only
visible here. Queries use the Application Insights REST API with an Entra token (Monitoring Reader on the resource)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from .config import Settings

AI_SCOPE = "https://api.applicationinsights.io/.default"
AI_API = "https://api.applicationinsights.io/v1/apps/{app}/query"


def enabled(settings: Settings) -> bool:
    return bool(settings.appinsights_app_id)


def query(settings: Settings, kql: str, timespan: str = "PT2H") -> list[dict[str, Any]]:
    from .foundry import credential

    token = credential().get_token(AI_SCOPE).token
    r = requests.post(AI_API.format(app=settings.appinsights_app_id), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"query": kql, "timespan": timespan}, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Application Insights query failed: {r.status_code} {r.text[:300]}")
    tables = r.json().get("tables") or []
    if not tables:
        return []
    cols = [c["name"] for c in tables[0]["columns"]]
    return [dict(zip(cols, row)) for row in tables[0]["rows"]]


SPAN_COLUMNS = ("timestamp, name, operation_Id, operation_ParentId, id, duration, "
                "agent=tostring(customDimensions['gen_ai.agent.name']), op=tostring(customDimensions['gen_ai.operation.name']), "
                "model=tostring(customDimensions['gen_ai.response.model']), rid=tostring(customDimensions['gen_ai.response.id']), "
                "inp=toint(customDimensions['gen_ai.usage.input_tokens']), outp=toint(customDimensions['gen_ai.usage.output_tokens']), "
                "cached=toint(coalesce(customDimensions['gen_ai.usage.cache_read.input_tokens'], customDimensions['gen_ai.usage.cached_tokens'])), "
                "tool=tostring(customDimensions['gen_ai.tool.name']), conv=tostring(customDimensions['gen_ai.conversation.id'])")


def _parse_ts(s: str) -> datetime:
    """Application Insights timestamps: ISO 8601 with up to 7 fractional digits and a Z suffix."""
    import re

    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$", s.strip())
    if not m:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    base, frac, tz = m.groups()
    d = datetime.fromisoformat(base + (f".{(frac or '0')[:6].ljust(6, '0')}") + ("+00:00" if not tz or tz == "Z" else tz))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _kql_ts(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def spans_for_response(settings: Settings, response_id: str, timespan: str = "PT6H", question: str = "") -> list[dict[str, Any]]:
    """The concierge run's spans plus the specialist run it triggered.

    Foundry sometimes propagates trace context over A2A (specialist spans share the operation id) and sometimes starts a
    new operation. So: take everything in the concierge's operation; if no other agent shows up, look for the specialist's
    spans (agent name from the A2A tool name) inside the time window of the concierge's execute_tool span, and if several
    runs fall in that window (other users), keep the one whose input contains the delegated question."""
    kql = (f"let ops = dependencies | where customDimensions['gen_ai.response.id'] == '{response_id}' | distinct operation_Id; "
           f"dependencies | where operation_Id in (ops) | project {SPAN_COLUMNS} | order by timestamp asc")
    spans = query(settings, kql, timespan)
    if not spans:
        return []
    concierge = next((s.get("agent") for s in spans if s.get("op") == "invoke_agent"), None)
    if any(s.get("agent") and s.get("agent") != concierge for s in spans):
        return spans
    for ex in [s for s in spans if s.get("op") == "execute_tool" and "remote_a2a_a2a-" in (s.get("tool") or "")]:
        skill = ex["tool"].split("remote_a2a_a2a-", 1)[1].split(".", 1)[0]
        agent = f"bank-{skill}"
        start = _parse_ts(ex["timestamp"]) - timedelta(seconds=3)
        end = _parse_ts(ex["timestamp"]) + timedelta(milliseconds=float(ex.get("duration") or 0)) + timedelta(seconds=8)
        kql = (f"let ops = dependencies | where timestamp between (datetime({_kql_ts(start)}) .. datetime({_kql_ts(end)})) "
               f"| where customDimensions['gen_ai.agent.name'] == '{agent}' and customDimensions['gen_ai.operation.name'] == 'invoke_agent' "
               f"| project operation_Id, inmsg=substring(tostring(customDimensions['gen_ai.input.messages']), 0, 2000); "
               f"dependencies | where operation_Id in (ops | project operation_Id) | project {SPAN_COLUMNS}, inmsg=substring(tostring(customDimensions['gen_ai.input.messages']), 0, 2000) | order by timestamp asc")
        cands = query(settings, kql, timespan)
        by_op: dict[str, list[dict]] = {}
        for c in cands:
            by_op.setdefault(c["operation_Id"], []).append(c)
        if not by_op:
            continue
        chosen = None
        if len(by_op) > 1 and question:
            needle = question.strip()[:60]
            for op, rows in by_op.items():
                if any(needle and needle in (r.get("inmsg") or "") for r in rows):
                    chosen = rows
                    break
        if chosen is None:
            chosen = list(by_op.values())[0]
        spans = spans + chosen
    return spans


def summarize(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-agent token totals from the chat spans (invoke_agent spans carry no usage) plus tool calls and timings."""
    agents: dict[str, dict[str, Any]] = {}
    for sp in spans:
        a = sp.get("agent") or "?"
        st = agents.setdefault(a, {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "chat_calls": 0, "tools": [], "models": set(), "ms": 0.0, "response_ids": set()})
        if sp.get("op") == "chat" or (sp.get("inp") is not None and sp.get("op") != "invoke_agent"):
            st["input_tokens"] += int(sp.get("inp") or 0)
            st["output_tokens"] += int(sp.get("outp") or 0)
            st["cached_tokens"] += int(sp.get("cached") or 0)
            st["chat_calls"] += 1
        if sp.get("op") == "invoke_agent":
            st["ms"] = max(st["ms"], float(sp.get("duration") or 0))
        if sp.get("tool"):
            st["tools"].append(sp["tool"])
        if sp.get("model"):
            st["models"].add(sp["model"])
        if sp.get("rid"):
            st["response_ids"].add(sp["rid"])
    for st in agents.values():
        st["models"] = sorted(st["models"])
        st["response_ids"] = sorted(st["response_ids"])
        st["total_tokens"] = st["input_tokens"] + st["output_tokens"]
    return agents


def reconcile_trace(settings: Settings, trace: dict[str, Any], pricing: Optional[dict] = None) -> bool:
    """Fill trace['usage']['specialist'] (and the totals / cost) from the Foundry trace of a handoff answer.
    Returns True when the specialist's spans were found. Safe to call repeatedly; no-op unless handoff.usage_pending."""
    from .pricing import load_pricing, usage_cost

    ho = trace.get("handoff") or {}
    if not ho or not ho.get("usage_pending") or not trace.get("response_id"):
        return False
    question = ""
    for c in ho.get("calls") or []:
        if c.get("type") == "a2a_preview_call" and c.get("arguments"):
            try:
                parts = json.loads(c["arguments"]).get("message", {}).get("parts", [])
                question = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
            except Exception:  # noqa: BLE001
                question = str(c["arguments"])
            break
    spans = spans_for_response(settings, trace["response_id"], question=question)
    per_agent = summarize(spans)
    specialists = {a: st for a, st in per_agent.items() if a != ho.get("concierge") and st["chat_calls"]}
    if not specialists:
        ho["reconcile_attempts"] = int(ho.get("reconcile_attempts", 0)) + 1
        return False
    pricing = pricing or load_pricing(settings)
    agent_usage = trace.setdefault("usage", {}).get("agent") or {}
    spec_usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "reasoning_tokens": 0}
    spec_cost = 0.0
    details = []
    retrieval_calls = 0
    for name, st in specialists.items():
        u = {"input_tokens": st["input_tokens"], "output_tokens": st["output_tokens"], "cached_tokens": st["cached_tokens"]}
        model = (st["models"] or [""])[0]
        c = usage_cost(pricing, model, u)
        spec_cost += c["total_usd"]
        for k in ("input_tokens", "output_tokens", "cached_tokens"):
            spec_usage[k] += u[k]
        kb_calls = sum(1 for tl in st["tools"] if "knowledge_base" in tl)
        retrieval_calls += kb_calls
        details.append({"agent": name, "model": model, "usage": u, "cost": c, "ms": st["ms"], "tools": st["tools"], "response_ids": st["response_ids"]})
    spec_usage["total_tokens"] = spec_usage["input_tokens"] + spec_usage["output_tokens"]
    trace["usage"]["specialist"] = spec_usage
    trace["usage"]["total"] = {k: int(agent_usage.get(k, 0) or 0) + int(spec_usage.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")}
    retrieval = trace.setdefault("retrieval", {})
    retrieval["calls"] = int(retrieval.get("calls", 0) or 0) + retrieval_calls
    retrieval_usd = float(pricing.get("retrieval_per_call", 0) or 0) * retrieval_calls
    cost = trace.setdefault("cost", {})
    concierge_cost = float((cost.get("agent") or {}).get("total_usd", 0) or 0) + float((cost.get("router") or {}).get("total_usd", 0) or 0)
    cost["specialist"] = {"total_usd": spec_cost, "agents": details}
    cost["retrieval_usd"] = float(cost.get("retrieval_usd", 0) or 0) + retrieval_usd
    cost["total_usd"] = concierge_cost + spec_cost + cost["retrieval_usd"]
    cost["note"] = "concierge + specialist tokens from the Foundry trace (Application Insights)"
    tm = trace.setdefault("timings_ms", {})
    tm["specialist"] = int(max((d["ms"] for d in details), default=0))
    ho.update({"usage_pending": False, "trace_operation_id": spans[0]["operation_Id"] if spans else "", "specialist_agents": [d["agent"] for d in details]})
    return True
