"""Chat models on Azure OpenAI (the deployments of the Foundry account) and the usage bookkeeping around them."""
from __future__ import annotations

from typing import Any, Optional

import requests
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from .azure_auth import ARM_SCOPE, COGNITIVE_SCOPE, token, token_provider
from .config import Settings

USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")
# A healthy answer streams its first token within seconds and its chunks milliseconds apart; the SDK default of 600 s
# let one stalled stream hold a customer for minutes (seen once in Azure). The read timeout applies between chunks.
MODEL_TIMEOUT_S = 90.0
MODEL_MAX_RETRIES = 2


def chat_model(settings: Settings, model: str, *, temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> BaseChatModel:
    """An `AzureChatOpenAI` bound to one deployment; the key from .env, or the signed-in identity when there is none."""
    from langchain_openai import AzureChatOpenAI

    settings.require("aoai_endpoint")
    kw: dict[str, Any] = dict(azure_endpoint=settings.aoai_endpoint, azure_deployment=model, model=model, api_version=settings.aoai_api_version,
                              stream_usage=True, timeout=MODEL_TIMEOUT_S, max_retries=MODEL_MAX_RETRIES)
    if settings.aoai_api_key:
        kw["api_key"] = settings.aoai_api_key
    else:
        kw["azure_ad_token_provider"] = token_provider(COGNITIVE_SCOPE)
    if temperature is not None:
        kw["temperature"] = temperature
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    return AzureChatOpenAI(**kw)


def usage_from_message(msg: Any) -> dict[str, int]:
    """Token usage of one AIMessage as plain ints (same keys the traces and pricing used before)."""
    u = getattr(msg, "usage_metadata", None) if not isinstance(msg, dict) else msg
    if not u:
        return {}
    ind = u.get("input_token_details") or {}
    outd = u.get("output_token_details") or {}
    out = {
        "input_tokens": u.get("input_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
        "total_tokens": u.get("total_tokens") or 0,
        "cached_tokens": ind.get("cache_read") or 0,
        "reasoning_tokens": outd.get("reasoning") or 0,
    }
    return {k: int(v) for k, v in out.items()}


def sum_usage(*usages: dict[str, int]) -> dict[str, int]:
    out = {k: 0 for k in USAGE_KEYS}
    for u in usages:
        for k in USAGE_KEYS:
            out[k] += int((u or {}).get(k, 0) or 0)
    return out


def response_model(msg: AIMessage | None) -> str:
    meta = getattr(msg, "response_metadata", None) or {}
    return str(meta.get("model_name") or meta.get("model") or "")


def response_id(msg: AIMessage | None) -> str:
    meta = getattr(msg, "response_metadata", None) or {}
    return str(meta.get("id") or getattr(msg, "id", "") or "")


ARM_DEPLOYMENTS_API = "2024-10-01"


def list_deployments(settings: Settings) -> list[dict[str, str]]:
    """Model deployments of the account, from ARM (the caller needs Reader on the account). Raises on failure."""
    settings.require("subscription_id", "resource_group", "foundry_account")
    url = f"https://management.azure.com{settings.account_resource_id}/deployments?api-version={ARM_DEPLOYMENTS_API}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token(ARM_SCOPE)}"}, timeout=30)
    r.raise_for_status()
    items = []
    for d in r.json().get("value", []):
        props = d.get("properties") or {}
        model = props.get("model") or {}
        items.append({"name": str(d.get("name") or ""), "model": str(model.get("name") or ""), "publisher": str(model.get("format") or ""),
                      "type": str((d.get("sku") or {}).get("name") or "")})
    return items
