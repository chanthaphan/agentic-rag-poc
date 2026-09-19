"""Model price table (pricing.yaml) and cost computation for a turn's token usage."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Settings

# Speech bills its own way: a realtime model charges for AUDIO tokens at roughly ten times its text rate, so a voice
# turn costed on the text rates alone reads as almost free. Every row therefore carries audio rates as well, and a
# model that has none (an ordinary chat model) simply never sees an audio token.
RATES = ("input", "cached_input", "output", "audio_input", "cached_audio_input", "audio_output")
DEFAULT = {"input": 1.0, "cached_input": 0.25, "output": 4.0, "audio_input": 0.0, "cached_audio_input": 0.0, "audio_output": 0.0}


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
        clean["models"][str(name)] = {k: float((row or {}).get(k, 0) or 0) for k in RATES}
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
    return {k: float(row.get(k, 0) or 0) for k in RATES}


def _int(d: Any, *keys: str) -> int:
    for k in keys:
        d = (d or {}).get(k) if isinstance(d, dict) else None
    try:
        return int(d or 0)
    except (TypeError, ValueError):
        return 0


def split_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Text and audio tokens, cached and not, from either a chat usage record or a realtime one.

    A realtime response reports `input_token_details` / `output_token_details` with the audio split inside them; a
    chat completion reports totals only, and those are all text."""
    in_total, out_total = _int(usage, "input_tokens"), _int(usage, "output_tokens")
    det_in, det_out = usage.get("input_token_details") or {}, usage.get("output_token_details") or {}
    if not det_in and not det_out:
        cached = _int(usage, "cached_tokens")
        return {"text_in": max(0, in_total - cached), "cached_text_in": cached, "audio_in": 0, "cached_audio_in": 0,
                "text_out": out_total, "audio_out": 0}
    cached_det = det_in.get("cached_tokens_details") or {}
    audio_in, text_in = _int(det_in, "audio_tokens"), _int(det_in, "text_tokens")
    cached_audio = _int(cached_det, "audio_tokens")
    cached_text = _int(cached_det, "text_tokens")
    if not cached_det:  # only a lump sum of cached tokens: bill them at the audio rate, which is the larger share
        cached_audio = min(audio_in, _int(det_in, "cached_tokens"))
        cached_text = max(0, _int(det_in, "cached_tokens") - cached_audio)
    audio_out = _int(det_out, "audio_tokens")
    text_out = _int(det_out, "text_tokens") or max(0, out_total - audio_out)
    return {"text_in": max(0, text_in - cached_text), "cached_text_in": cached_text,
            "audio_in": max(0, audio_in - cached_audio), "cached_audio_in": cached_audio,
            "text_out": text_out, "audio_out": audio_out}


def usage_cost(pricing: dict[str, Any], model: str, usage: dict[str, Any]) -> dict[str, float]:
    """What one model call cost. Cached tokens bill at the cached rate, audio tokens at the audio rate."""
    p = price_for(pricing, model)
    u = split_usage(usage or {})
    cost = {
        "input_usd": u["text_in"] * p["input"] / 1e6,
        "cached_usd": u["cached_text_in"] * p["cached_input"] / 1e6 + u["cached_audio_in"] * p["cached_audio_input"] / 1e6,
        "output_usd": u["text_out"] * p["output"] / 1e6,
        "audio_input_usd": u["audio_in"] * p["audio_input"] / 1e6,
        "audio_output_usd": u["audio_out"] * p["audio_output"] / 1e6,
    }
    cost["total_usd"] = sum(v for k, v in cost.items() if k.endswith("_usd"))
    cost["model"] = model
    return cost


def voice_turn_cost(pricing: dict[str, Any], model: str, usage: dict[str, Any]) -> dict[str, Any]:
    """A spoken turn's cost, in the shape a turn's trace carries (so Studio totals it like any other answer)."""
    agent = usage_cost(pricing, model, usage or {})
    return {"agent": agent, "router": {"total_usd": 0.0, "model": ""}, "retrieval_usd": 0.0,
            "total_usd": agent["total_usd"], "currency": pricing.get("currency", "USD")}


def turn_cost(pricing: dict[str, Any], router_model: str, agent_model: str, usage: dict[str, dict], retrieval_calls: int = 0) -> dict[str, Any]:
    router = usage_cost(pricing, router_model, usage.get("router") or {})
    agent = usage_cost(pricing, agent_model, usage.get("agent") or {})
    retrieval = float(pricing.get("retrieval_per_call", 0) or 0) * retrieval_calls
    return {"router": router, "agent": agent, "retrieval_usd": retrieval, "total_usd": router["total_usd"] + agent["total_usd"] + retrieval, "currency": pricing.get("currency", "USD")}
