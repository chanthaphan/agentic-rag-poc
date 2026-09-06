"""Model price table (pricing.yaml) and cost computation for a turn's token usage."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Settings

DEFAULT = {"input": 1.0, "cached_input": 0.25, "output": 4.0}


def pricing_path(settings: Settings) -> Path:
    return settings.pricing_file


def load_pricing(settings: Settings) -> dict[str, Any]:
    p = pricing_path(settings)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    data = data or {}
    data.setdefault("currency", "USD")
    data.setdefault("models", {})
    data["models"].setdefault("default", dict(DEFAULT))
    data.setdefault("retrieval_per_call", 0.0)
    return data


def save_pricing(settings: Settings, data: dict[str, Any]) -> dict[str, Any]:
    clean = {"currency": str(data.get("currency", "USD")), "models": {}, "retrieval_per_call": float(data.get("retrieval_per_call", 0) or 0)}
    for name, row in (data.get("models") or {}).items():
        if not name:
            continue
        clean["models"][str(name)] = {k: float((row or {}).get(k, 0) or 0) for k in ("input", "cached_input", "output")}
    clean["models"].setdefault("default", dict(DEFAULT))
    pricing_path(settings).write_text(yaml.safe_dump(clean, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return clean


def price_for(pricing: dict[str, Any], model: str) -> dict[str, float]:
    models = pricing.get("models", {})
    row = models.get(model)
    if row is None and model:
        # longest configured prefix wins (e.g. "gpt-4.1-mini-2025-04-14" -> "gpt-4.1-mini", not "gpt-4.1")
        candidates = [k for k in models if k and k != "default" and model.startswith(k)]
        if candidates:
            row = models[max(candidates, key=len)]
    row = row or models.get("default", DEFAULT)
    return {k: float(row.get(k, 0) or 0) for k in ("input", "cached_input", "output")}


def usage_cost(pricing: dict[str, Any], model: str, usage: dict[str, int]) -> dict[str, float]:
    """usage = {input_tokens, output_tokens, cached_tokens}; cached tokens are billed at the cached rate."""
    p = price_for(pricing, model)
    cached = int(usage.get("cached_tokens", 0) or 0)
    uncached = max(0, int(usage.get("input_tokens", 0) or 0) - cached)
    out = int(usage.get("output_tokens", 0) or 0)
    cost = {
        "input_usd": uncached * p["input"] / 1e6,
        "cached_usd": cached * p["cached_input"] / 1e6,
        "output_usd": out * p["output"] / 1e6,
    }
    cost["total_usd"] = cost["input_usd"] + cost["cached_usd"] + cost["output_usd"]
    cost["model"] = model
    return cost


def turn_cost(pricing: dict[str, Any], router_model: str, agent_model: str, usage: dict[str, dict], retrieval_calls: int = 0) -> dict[str, Any]:
    router = usage_cost(pricing, router_model, usage.get("router") or {})
    agent = usage_cost(pricing, agent_model, usage.get("agent") or {})
    retrieval = float(pricing.get("retrieval_per_call", 0) or 0) * retrieval_calls
    return {"router": router, "agent": agent, "retrieval_usd": retrieval, "total_usd": router["total_usd"] + agent["total_usd"] + retrieval, "currency": pricing.get("currency", "USD")}
