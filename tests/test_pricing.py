from pathlib import Path

from bankrag.config import Settings
from bankrag.pricing import load_pricing, turn_cost, usage_cost

ROOT = Path(__file__).resolve().parents[1]


def test_cost_computation():
    pricing = load_pricing(Settings.load(ROOT))
    c = usage_cost(pricing, "gpt-4.1-mini", {"input_tokens": 1_000_000, "cached_tokens": 500_000, "output_tokens": 100_000})
    assert round(c["input_usd"], 4) == 0.2 and round(c["cached_usd"], 4) == 0.05 and round(c["output_usd"], 4) == 0.16
    assert round(c["total_usd"], 4) == 0.41
    t = turn_cost(pricing, "gpt-4.1-mini", "unknown-model", {"router": {"input_tokens": 1000, "output_tokens": 40}, "agent": {"input_tokens": 18000, "output_tokens": 200}}, 1)
    assert t["agent"]["model"] == "unknown-model" and t["total_usd"] > 0 and t["currency"] == "USD"


def test_longest_prefix_model_match():
    from bankrag.pricing import price_for

    pricing = load_pricing(Settings.load(ROOT))
    assert price_for(pricing, "gpt-4.1-mini-2025-04-14") == price_for(pricing, "gpt-4.1-mini")
    assert price_for(pricing, "gpt-4.1-2025-04-14") == price_for(pricing, "gpt-4.1")
    assert price_for(pricing, "totally-unknown") == price_for(pricing, "default")
