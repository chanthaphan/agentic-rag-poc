"""Settings loaded from environment / .env."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required setting is missing."""


OVERLAY_KEYS = ("SUGGESTIONS_MODE", "SUGGESTIONS_MODEL", "ROUTER_MODEL", "DEFAULT_CHAT_MODEL", "KB_REASONING_EFFORT", "KB_MAX_OUTPUT_TOKENS", "ASSISTANT_NAME", "APP_USER_NAME", "APP_USER_INITIALS", "ASSISTANT_NAME_EN", "KB_LLM_DEPLOYMENT", "JUDGE_MODEL", "ORCHESTRATION_MODE", "CONCIERGE_MODEL", "HISTORY_TURNS")
_overlay: dict[str, str] = {}


def _env(name: str, default: str = "") -> str:
    if name in _overlay and str(_overlay[name]).strip():
        return str(_overlay[name]).strip()
    return os.environ.get(name, default).strip()


def _orchestration_mode(value: str) -> str:
    v = (value or "").strip().lower() or "router"
    return "supervisor" if v in ("supervisor", "a2a", "handoff", "concierge") else "router"


def overlay_path(root: Path) -> Path:
    return root / os.environ.get("STATE_DIR", ".state") / "settings.json"  # absolute STATE_DIR wins in Path joining


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
    foundry_account: str  # the Azure OpenAI / Foundry account that holds the model deployments
    # Azure OpenAI
    aoai_endpoint: str
    aoai_api_key: str
    aoai_api_version: str
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
    judge_model: str
    orchestration_mode: str  # router | supervisor
    concierge_model: str
    history_turns: int  # how many earlier question/answer pairs the agent sees; older ones become a short recap
    suggestions_mode: str  # dynamic: written from the turn | static: the skill's frontmatter list
    suggestions_model: str
    bbl_api_subscription: str  # subscription value the bangkokbank.com site sends with its own public API calls (FX, branches)
    bbl_api_base: str
    services_mcp_key: str  # local-dev fallback guard for /mcp/services; in Azure the caller is checked by identity
    services_mcp_callers: list[str]  # object ids allowed to call /mcp/services (external MCP clients)
    public_base_url: str  # public https base of this app (Host allow-list of /mcp/services)
    public_base_aliases: list[str]  # other names this app answers on, so a domain move does not 421 mid-cutover
    google_maps_key: str  # optional: with it a place card shows an embedded map, without it a link that opens Maps
    kb_mcp_auth: str
    kb_transport: str  # rest: one retrieve call | mcp: the knowledge base's MCP endpoint (a session per call)
    kb_max_output_tokens: int
    # App
    studio_password: str
    studio_admins: list[str]
    studio_testers: list[str]
    studio_externals: list[str]
    app_user_name: str
    app_user_initials: str
    assistant_name: str  # the persona's Thai name; {assistant_name} in the prompts, the app title and greeting
    assistant_name_en: str  # the same persona in English; {assistant_name_en} in the prompts and the English greeting
    root: Path
    skills_dir: Path
    knowledge_dir: Path
    rules_dir: Path
    state_dir: Path
    evals_dir: Path
    pricing_file: Path
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
            aoai_endpoint=_env("AOAI_ENDPOINT").rstrip("/"),
            aoai_api_key=_env("AOAI_API_KEY"),
            aoai_api_version=_env("AOAI_API_VERSION", "2025-04-01-preview"),
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
            judge_model=_env("JUDGE_MODEL", "gpt-4.1-mini"),
            orchestration_mode=_orchestration_mode(_env("ORCHESTRATION_MODE", "router")),
            concierge_model=_env("CONCIERGE_MODEL", ""),
            history_turns=int(_env("HISTORY_TURNS", "6") or "6"),
            suggestions_mode=(_env("SUGGESTIONS_MODE", "dynamic").strip().lower() or "dynamic"),
            suggestions_model=_env("SUGGESTIONS_MODEL", ""),
            bbl_api_subscription=_env("BBL_API_KEY", ""),
            bbl_api_base=_env("BBL_API_BASE", "https://www.bangkokbank.com/api").rstrip("/"),
            services_mcp_key=_env("MCP_TOOL_KEY", ""),
            services_mcp_callers=[p.strip() for p in _env("MCP_CALLER_PRINCIPALS", "").split(",") if p.strip()],
            public_base_url=_env("PUBLIC_BASE_URL", "").rstrip("/"),
            public_base_aliases=[u.strip().rstrip("/") for u in _env("PUBLIC_BASE_ALIASES", "").split(",") if u.strip()],
            google_maps_key=_env("GOOGLE_MAPS_KEY", ""),
            kb_mcp_auth=_env("KB_MCP_AUTH", "identity"),
            kb_transport=(_env("KB_TRANSPORT", "rest").strip().lower() or "rest"),
            kb_max_output_tokens=int(_env("KB_MAX_OUTPUT_TOKENS", "0")),
            studio_password=_env("STUDIO_PASSWORD"),
            studio_admins=[e.strip().lower() for e in _env("STUDIO_ADMINS", "").split(",") if e.strip()],
            studio_testers=[e.strip().lower() for e in _env("STUDIO_TESTERS", "").split(",") if e.strip()],
            studio_externals=[e.strip().lower() for e in _env("STUDIO_EXTERNALS", "").split(",") if e.strip()],
            app_user_name=_env("APP_USER_NAME", "Pim"),
            app_user_initials=_env("APP_USER_INITIALS", "PW"),
            assistant_name=_env("ASSISTANT_NAME", "เกรส"),
            assistant_name_en=_env("ASSISTANT_NAME_EN", "Grace"),
            root=root,
            skills_dir=root / _env("SKILLS_DIR", "skills"),
            knowledge_dir=root / _env("KNOWLEDGE_DIR", "knowledge"),
            rules_dir=root / _env("RULES_DIR", "rules"),
            state_dir=root / _env("STATE_DIR", ".state"),
            evals_dir=root / _env("EVALS_DIR", "evals"),
            pricing_file=root / _env("PRICING_FILE", "pricing.yaml"),
            api_port=int(_env("API_PORT", "8010")),
        )

    # ---- derived ----
    @property
    def account_resource_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self.foundry_account}"
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
        self.rules_dir.mkdir(parents=True, exist_ok=True)
