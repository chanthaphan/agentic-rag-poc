"""Foundry project connections (ARM) for the knowledge-base MCP endpoints."""
from __future__ import annotations

import requests
from azure.core.credentials import TokenCredential

from .config import Settings
from .models import SkillSpec

ARM = "https://management.azure.com"
API_VERSION = "2025-10-01-preview"


def _headers(credential: TokenCredential) -> dict[str, str]:
    token = credential.get_token("https://management.azure.com/.default").token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def connection_url(settings: Settings, name: str) -> str:
    return f"{ARM}{settings.project_resource_id}/connections/{name}?api-version={API_VERSION}"


def ensure_kb_connection(settings: Settings, spec: SkillSpec, credential: TokenCredential) -> dict:
    """RemoteTool connection using the project's managed identity against the KB MCP endpoint (idempotent PUT)."""
    body = {
        "name": spec.connection_name,
        "type": "Microsoft.MachineLearningServices/workspaces/connections",
        "properties": {
            "authType": "ProjectManagedIdentity",
            "category": "RemoteTool",
            "target": settings.kb_mcp_url(spec.kb_name),
            "isSharedToAll": True,
            "audience": "https://search.azure.com/",
            "metadata": {"ApiType": "Azure"},
        },
    }
    r = requests.put(connection_url(settings, spec.connection_name), headers=_headers(credential), json=body, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"connection PUT {spec.connection_name} failed: {r.status_code} {r.text[:500]}")
    return r.json()


SERVICES_CONNECTION = "bank-services-mcp"


def ensure_services_connection(settings: Settings, credential: TokenCredential) -> dict:
    """RemoteTool connection to our own /mcp/services endpoint, authenticated with the project's managed identity.

    The endpoint sits behind the app's Easy Auth, so the agent needs a token for the Easy Auth app registration:
    `audience` must be the api://<client-id> that `infra/12-easyauth.sh` set as the allowed token audience. The app
    then checks the authenticated object id against MCP_CALLER_PRINCIPALS."""
    from .mcp_server import MCP_PATH

    settings.require("public_base_url", "services_mcp_audience")
    body = {
        "name": SERVICES_CONNECTION,
        "type": "Microsoft.MachineLearningServices/workspaces/connections",
        "properties": {
            "authType": "ProjectManagedIdentity",
            "category": "RemoteTool",
            "target": f"{settings.public_base_url}{MCP_PATH}/",
            "isSharedToAll": True,
            "audience": settings.services_mcp_audience,
            "metadata": {"ApiType": "Azure"},
        },
    }
    r = requests.put(connection_url(settings, SERVICES_CONNECTION), headers=_headers(credential), json=body, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"connection PUT {SERVICES_CONNECTION} failed: {r.status_code} {r.text[:500]}")
    return r.json()


def get_connection(settings: Settings, name: str, credential: TokenCredential) -> dict | None:
    r = requests.get(connection_url(settings, name), headers=_headers(credential), timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def delete_connection(settings: Settings, name: str, credential: TokenCredential) -> bool:
    r = requests.delete(connection_url(settings, name), headers=_headers(credential), timeout=60)
    return r.status_code in (200, 202, 204)
