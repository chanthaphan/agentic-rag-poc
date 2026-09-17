"""Entra identity for the app's own calls: Azure OpenAI (when no key is set), the knowledge-base MCP endpoint, ARM."""
from __future__ import annotations

from functools import lru_cache
from typing import Callable

from azure.identity import DefaultAzureCredential, get_bearer_token_provider

SEARCH_SCOPE = "https://search.azure.com/.default"
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
ARM_SCOPE = "https://management.azure.com/.default"


@lru_cache(maxsize=1)
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def token(scope: str) -> str:
    """A bearer token for `scope` (DefaultAzureCredential caches and refreshes it)."""
    return credential().get_token(scope).token


def token_provider(scope: str) -> Callable[[], str]:
    return get_bearer_token_provider(credential(), scope)
