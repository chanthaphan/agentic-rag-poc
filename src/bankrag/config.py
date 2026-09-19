"""Settings loaded from environment / .env."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required setting is missing."""


OVERLAY_KEYS = ("SUGGESTIONS_MODE", "SUGGESTIONS_MODEL", "ROUTER_MODEL", "DEFAULT_CHAT_MODEL", "KB_REASONING_EFFORT", "KB_MAX_OUTPUT_TOKENS", "ASSISTANT_NAME", "APP_USER_NAME", "APP_USER_INITIALS", "ASSISTANT_NAME_EN", "ASSISTANT_GENDER", "KB_LLM_DEPLOYMENT", "JUDGE_MODEL", "ORCHESTRATION_MODE", "CONCIERGE_MODEL", "HISTORY_TURNS", "REALTIME_DEPLOYMENT", "REALTIME_VOICE", "REALTIME_TRANSCRIBE_MODEL", "REALTIME_AVATAR_URL", "TTS_DEPLOYMENT", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY")
_overlay: dict[str, str] = {}


def _env(name: str, default: str = "") -> str:
    if name in _overlay and str(_overlay[name]).strip():
        return str(_overlay[name]).strip()
    return os.environ.get(name, default).strip()


# Where the chat models are called. Four ways a team can bring their own key:
#   azure       the Azure OpenAI account in AOAI_ENDPOINT (a key, or the signed-in identity)
#   openai      OpenAI directly, on api.openai.com
#   compatible  anything speaking the OpenAI API: LiteLLM, OpenRouter, vLLM, an internal gateway (needs a base URL)
#   anthropic   Anthropic directly (chat only: speech mode has no audio models there and stays on Azure/OpenAI)
PROVIDERS = ("azure", "openai", "compatible", "anthropic")
OPENAI_BASE = "https://api.openai.com/v1"


def _provider(value: str) -> str:
    v = (value or "").strip().lower()
    if v in ("litellm", "openrouter", "vllm", "gateway", "custom", "external", "compatible"):
        return "compatible"
    if v in ("claude", "anthropic"):
        return "anthropic"
    return v if v in PROVIDERS else "azure"


def _gender(value: str) -> str:
    """male unless the setting clearly says female: the persona's Thai particles and pronoun follow it."""
    return "female" if (value or "").strip().lower() in ("female", "f", "woman", "หญิง") else "male"


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
    assistant_gender: str  # male | female: fills {gender_word}, {particle}, {particle_q}, {particle_soft}, {pronoun_th}, {wrong_particles}
    # Speech mode (Azure OpenAI Realtime over WebRTC)
    realtime_deployment: str  # the realtime deployment the voice page talks to; empty turns speech mode off
    realtime_voice: str  # the model's voice: alloy, ash, ballad, cedar, coral, echo, marin, sage, shimmer, verse
    realtime_transcribe_model: str  # transcribes what the customer said, for the captions and the saved turn
    realtime_avatar_url: str  # GLB the voice page renders; empty = the built-in three.js avatar
    tts_deployment: str  # reads an answer aloud on play; an audio model (gpt-audio) keeps the call's voice, a tts one is also accepted
    # Bringing your own model service: a key pasted in Studio overrides the deployment's own, and with provider
    # "openai" the models are called on any OpenAI-compatible endpoint instead of the Azure account.
    llm_provider: str  # azure | openai
    llm_base_url: str  # for provider "openai": https://api.openai.com/v1 or a compatible gateway
    llm_api_key: str   # the key the app uses; empty falls back to AOAI_API_KEY, then the signed-in identity
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
            # a persona's name is configuration, not code: unset, the assistant is simply "the assistant"
            assistant_name=_env("ASSISTANT_NAME", "ผู้ช่วย"),
            assistant_name_en=_env("ASSISTANT_NAME_EN", "Assistant"),
            assistant_gender=_gender(_env("ASSISTANT_GENDER", "male")),
            realtime_deployment=_env("REALTIME_DEPLOYMENT", "gpt-realtime-2.1"),
            realtime_voice=_env("REALTIME_VOICE", "cedar"),
            realtime_transcribe_model=_env("REALTIME_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
            realtime_avatar_url=_env("REALTIME_AVATAR_URL", ""),
            tts_deployment=_env("TTS_DEPLOYMENT", "gpt-audio-1.5"),
            llm_provider=_provider(_env("LLM_PROVIDER", "azure")),
            llm_base_url=_env("LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=_env("LLM_API_KEY", ""),
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
    def model_key(self) -> str:
        """The key the models are called with: the one bound in Studio, else the deployment's own."""
        return self.llm_api_key or self.aoai_api_key

    @property
    def chat_base_url(self) -> str:
        """Where chat calls go for an OpenAI-shaped service (OpenAI itself defaults to its own address)."""
        if self.llm_provider == "openai":
            return self.llm_base_url or OPENAI_BASE
        return self.llm_base_url

    @property
    def uses_own_service(self) -> bool:
        """True when the chat models run somewhere other than the Azure account bound to this app."""
        if self.llm_provider == "anthropic":
            return bool(self.llm_api_key)
        return self.llm_provider in ("openai", "compatible") and bool(self.chat_base_url)

    @property
    def speech_service(self) -> str:
        """Speech mode needs realtime and audio models, which only Azure OpenAI and OpenAI have. A gateway or
        Anthropic serves the chat while the voice stays on the Azure account this app was deployed with."""
        return "openai" if self.llm_provider == "openai" and self.llm_api_key else "azure"

    @property
    def aoai_v1_base_url(self) -> str:
        """The v1 surface the realtime, speech and embedding calls use."""
        return (self.llm_base_url or OPENAI_BASE) if self.speech_service == "openai" else f"{self.aoai_endpoint}/openai/v1"

    @property
    def realtime_client_secrets_url(self) -> str:
        """Where the app mints the short-lived key the browser uses for its WebRTC call (GA surface, no api-version)."""
        return f"{self.aoai_v1_base_url}/realtime/client_secrets"

    @property
    def speech_url(self) -> str:
        """Where an answer is turned into audio to play back (the same v1 surface the realtime session uses)."""
        return f"{self.aoai_v1_base_url}/audio/speech"

    @property
    def realtime_calls_url(self) -> str:
        """Where the browser posts its SDP offer; the audio then flows browser to Azure, never through this app."""
        return f"{self.aoai_v1_base_url}/realtime/calls"

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
