"""The model factory, usage bookkeeping and the deployments list."""
from pathlib import Path

from bankrag import llm as L
from bankrag.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _settings(**over):
    s = Settings.load(ROOT)
    s.aoai_endpoint = "https://acct.openai.azure.com"
    s.aoai_api_key = "k"
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_chat_model_uses_the_key_or_the_identity(monkeypatch):
    captured = {}

    class Fake:
        def __init__(self, **kw):
            captured.update(kw)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "AzureChatOpenAI", Fake)
    L.chat_model(_settings(), "gpt-4.1-mini", temperature=0)
    assert captured["azure_endpoint"] == "https://acct.openai.azure.com" and captured["azure_deployment"] == "gpt-4.1-mini"
    assert captured["api_key"] == "k" and captured["stream_usage"] is True and captured["temperature"] == 0 and "azure_ad_token_provider" not in captured
    assert captured["timeout"] == L.MODEL_TIMEOUT_S and captured["max_retries"] == L.MODEL_MAX_RETRIES  # a stalled stream fails fast
    captured.clear()
    monkeypatch.setattr(L, "token_provider", lambda scope: f"provider:{scope}")
    L.chat_model(_settings(aoai_api_key=""), "gpt-5.1-chat")
    assert captured["azure_ad_token_provider"] == "provider:https://cognitiveservices.azure.com/.default" and "api_key" not in captured and "temperature" not in captured


def test_usage_from_message_and_sum():
    from langchain_core.messages import AIMessage

    m = AIMessage(content="x", usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                                                "input_token_details": {"cache_read": 40}, "output_token_details": {"reasoning": 5}})
    assert L.usage_from_message(m) == {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cached_tokens": 40, "reasoning_tokens": 5}
    assert L.usage_from_message(AIMessage(content="x")) == {} and L.usage_from_message(None) == {}
    assert L.sum_usage({"input_tokens": 1}, {}, {"input_tokens": 2, "output_tokens": 3})["input_tokens"] == 3
    from bankrag.router import usage_dict

    assert usage_dict(m)["cached_tokens"] == 40 and usage_dict(None) == {}


def test_list_deployments_parses_arm(monkeypatch):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"value": [{"name": "gpt-4.1-mini", "properties": {"model": {"name": "gpt-4.1-mini", "format": "OpenAI"}}, "sku": {"name": "GlobalStandard"}},
                              {"name": "text-embedding-3-large", "properties": {"model": {"name": "text-embedding-3-large", "format": "OpenAI"}}, "sku": {"name": "Standard"}}]}

    seen = {}
    monkeypatch.setattr(L, "token", lambda scope: "t")
    monkeypatch.setattr(L.requests, "get", lambda url, headers, timeout: seen.update(url=url, headers=headers) or R())
    s = _settings(subscription_id="sub", resource_group="rg", foundry_account="acct")
    items = L.list_deployments(s)
    assert [i["name"] for i in items] == ["gpt-4.1-mini", "text-embedding-3-large"] and items[0]["publisher"] == "OpenAI" and items[0]["type"] == "GlobalStandard"
    assert "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/deployments" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer t"


def test_models_route_falls_back_to_the_configured_models(monkeypatch):
    from fastapi.testclient import TestClient

    from bankrag import api

    def boom(settings):
        raise RuntimeError("no arm")

    monkeypatch.setattr(L, "list_deployments", boom)
    api._models_cache.update(at=0.0, items=[])
    items = TestClient(api.app).get("/app/models?refresh=1").json()
    assert items and items[0]["name"] == api.settings.default_chat_model and items[0]["error"] == "RuntimeError"


def test_the_model_list_can_be_asked_for_the_realtime_deployments(monkeypatch):
    """The chat pickers hide realtime deployments; the speech-mode picker is the one place that wants them."""
    from fastapi.testclient import TestClient

    from bankrag import api

    monkeypatch.setattr(L, "list_deployments", lambda s: [{"name": "gpt-4.1-mini"}, {"name": "gpt-realtime-2.1"}, {"name": "text-embedding-3-large"}])
    api._models_cache.update(at=0.0, items=[])
    c = TestClient(api.app)
    assert [m["name"] for m in c.get("/app/models?refresh=1").json()] == ["gpt-4.1-mini"]
    assert [m["name"] for m in c.get("/app/models?kind=realtime").json()] == ["gpt-realtime-2.1"]

    def boom(settings):
        raise RuntimeError("no arm")

    monkeypatch.setattr(L, "list_deployments", boom)
    monkeypatch.setattr(api.settings, "realtime_deployment", "gpt-realtime-1.5")
    api._models_cache.update(at=0.0, items=[])
    assert [m["name"] for m in c.get("/app/models?refresh=1&kind=realtime").json()] == ["gpt-realtime-1.5"]
