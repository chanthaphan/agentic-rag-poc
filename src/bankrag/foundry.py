"""Shared Foundry client helpers."""
from __future__ import annotations

from functools import lru_cache

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

from .config import Settings


@lru_cache(maxsize=1)
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def project_client(settings: Settings) -> AIProjectClient:
    settings.require("project_endpoint")
    return AIProjectClient(endpoint=settings.project_endpoint, credential=credential(), allow_preview=True)
