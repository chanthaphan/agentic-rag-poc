"""Settings loaded from environment / .env."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required setting is missing."""


OVERLAY_KEYS = ("ROUTER_MODEL", "DEFAULT_CHAT_MODEL", "KB_REASONING_EFFORT", "KB_MAX_OUTPUT_TOKENS", "ASSISTANT_NAME", "APP_USER_NAME", "APP_USER_INITIALS", "KB_LLM_DEPLOYMENT")
_overlay: dict[str, str] = {}


def _env(name: str, default: str = "") -> str:
    if name in _overlay and str(_overlay[name]).strip():
        return str(_overlay[name]).strip()
    return os.environ.get(name, default).strip()


def overlay_path(root: Path) -> Path:
    return root / os.environ.get("STATE_DIR", ".state") / "settings.json"


def load_overlay(root: Path) -> dict[str, str]:
    """Runtime settings saved from Studio (.state/settings.json) override .env for OVERLAY_KEYS."""
    p = overlay_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {k: str(v) for k, v in data.items() if k in OVERLAY_KEYS and v is not None}


def save_overlay(root: Path, values: dict[str, str]) -> dict[str, str]:
    p = overlay_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_overlay(root)
    for k, v in values.items():
        if k in OVERLAY_KEYS:
            if v in (None, ""):
                current.pop(k, None)
            else:
                current[k] = str(v)
    p.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    return current


@dataclass
class Settings:
    # Azure / ARM
    tenant_id: str
    subscription_id: str
    resource_group: str
    foundry_account: str
    foundry_project: str
    # Foundry / models
    project_endpoint: str
    aoai_endpoint: str
    aoai_api_key: str
    embed_deployment: str
    embed_dims: int
    default_chat_model: str
    router_model: str
    # Search
    search_service_name: str
    search_endpoint: str
    search_admin_key: str
    search_query_key: str
    search_index: str
    search_api_version: str
    # Knowledge base behaviour
    kb_reasoning_effort: str
    kb_llm_deployment: str
    kb_mcp_auth: str
    kb_max_output_tokens: int
    # App
    studio_password: str
    app_user_name: str
    app_user_initials: str
    assistant_name: str
    root: Path
    skills_dir: Path
    knowledge_dir: Path
    state_dir: Path
    api_port: int

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        root = (root or Path.cwd()).resolve()
        load_dotenv(root / ".env", override=False)
        _overlay.clear()
        _overlay.update(load_overlay(root))
        return cls(
            tenant_id=_env("AZURE_TENANT_ID"),
            subscription_id=_env("AZURE_SUBSCRIPTION_ID"),
            resource_group=_env("AZURE_RESOURCE_GROUP", "my-aiverse"),
            foundry_account=_env("FOUNDRY_ACCOUNT", "my-model-hub"),
            foundry_project=_env("FOUNDRY_PROJECT", "firstProject"),
            project_endpoint=_env("FOUNDRY_PROJECT_ENDPOINT").rstrip("/"),
            aoai_endpoint=_env("AOAI_ENDPOINT").rstrip("/"),
            aoai_api_key=_env("AOAI_API_KEY"),
            embed_deployment=_env("EMBED_DEPLOYMENT", "text-embedding-3-large"),
            embed_dims=int(_env("EMBED_DIMS", "3072")),
            default_chat_model=_env("DEFAULT_CHAT_MODEL", "gpt-4.1-mini"),
            router_model=_env("ROUTER_MODEL", "gpt-4.1-mini"),
            search_service_name=_env("SEARCH_SERVICE_NAME"),
            search_endpoint=_env("SEARCH_ENDPOINT").rstrip("/"),
            search_admin_key=_env("SEARCH_ADMIN_KEY"),
            search_query_key=_env("SEARCH_QUERY_KEY"),
            search_index=_env("SEARCH_INDEX", "bank-products"),
            search_api_version=_env("SEARCH_API_VERSION", "2026-08-01-preview"),
            kb_reasoning_effort=_env("KB_REASONING_EFFORT", "minimal"),
            kb_llm_deployment=_env("KB_LLM_DEPLOYMENT", "gpt-4.1-mini"),
            kb_mcp_auth=_env("KB_MCP_AUTH", "identity"),
            kb_max_output_tokens=int(_env("KB_MAX_OUTPUT_TOKENS", "0")),
            studio_password=_env("STUDIO_PASSWORD"),
            app_user_name=_env("APP_USER_NAME", "Pim"),
            app_user_initials=_env("APP_USER_INITIALS", "PW"),
            assistant_name=_env("ASSISTANT_NAME", "Assistant"),
            root=root,
            skills_dir=root / _env("SKILLS_DIR", "skills"),
            knowledge_dir=root / _env("KNOWLEDGE_DIR", "knowledge"),
            state_dir=root / _env("STATE_DIR", ".state"),
            api_port=int(_env("API_PORT", "8010")),
        )

    # ---- derived ----
    @property
    def project_resource_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self.foundry_account}/projects/{self.foundry_project}"
        )

    @property
    def search_resource_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.Search/searchServices/{self.search_service_name}"
        )

    @property
    def aoai_v1_base_url(self) -> str:
        return f"{self.aoai_endpoint}/openai/v1"

    def kb_mcp_url(self, kb_name: str) -> str:
        return f"{self.search_endpoint}/knowledgebases/{kb_name}/mcp?api-version={self.search_api_version}"

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(f"Missing settings in .env: {', '.join(missing)} (see .env.example)")

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
