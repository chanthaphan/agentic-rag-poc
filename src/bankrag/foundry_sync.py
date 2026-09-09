"""Sync skills/<id>/SKILL.md to Foundry: knowledge source + knowledge base + project connection + agent version."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, Optional

from azure.core.exceptions import ResourceNotFoundError
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition

from . import connections as CONN
from . import knowledge_base as KB
from . import rules as RL
from .ingest.progress import report as progress
from .config import Settings
from .foundry import credential, project_client
from .models import ROUTER_AGENT, SkillSpec, SyncReport, SyncRow
from .router import build_router_definition
from .search_index import index_client
from .skills import compose_instructions

Log = Callable[[str], None]
SOURCE_TAG = "bankrag"
STATE_FILE = "foundry_state.json"


# ---------- definitions ----------
def kb_tool(settings: Settings, spec: SkillSpec, kb_owner: Optional[SkillSpec] = None) -> MCPTool:
    """MCP tool for the skill's knowledge base; `kb_owner` overrides which skill's KB/connection is used (shared KB)."""
    owner = kb_owner or spec
    common = dict(
        server_label="knowledge-base",
        server_url=settings.kb_mcp_url(owner.kb_name),
        require_approval="never",
        allowed_tools=["knowledge_base_retrieve"],
    )
    if settings.kb_mcp_auth == "apikey":  # POC-only fallback: query key travels in the agent definition
        settings.require("search_query_key")
        return MCPTool(headers={"api-key": settings.search_query_key}, **common)
    return MCPTool(project_connection_id=owner.connection_name, **common)


TOOL_ONLY_NOTE = (
    "\n\n# Knowledge base status\n"
    "You have no product documents, and you do not need any: your answers come from your live tool(s) ({tools}). "
    "Call the tool for every question in your scope and answer from what it returns. Never say your knowledge base is "
    "empty, and never answer a live value from memory or from earlier in the conversation."
)


def services_tool(settings: Settings, spec: SkillSpec) -> Optional[MCPTool]:
    """Our own live-service MCP endpoint, for skills that declare `tools:` in their frontmatter.

    Needs PUBLIC_BASE_URL (the agent calls us from Foundry, so it must be the public https name) and MCP_TOOL_KEY,
    which travels in the agent definition the same way the KB query key does under KB_MCP_AUTH=apikey."""
    from .mcp_server import AUTH_HEADER, MCP_PATH

    if not spec.tools or not settings.public_base_url:
        return None
    common = dict(
        server_label="bank-services",
        server_url=f"{settings.public_base_url}{MCP_PATH}/",
        require_approval="never",
        allowed_tools=list(spec.tools),
    )
    if settings.services_mcp_audience:  # the agent authenticates as the project's managed identity
        return MCPTool(project_connection_id=CONN.SERVICES_CONNECTION, **common)
    if settings.services_mcp_key:  # local / no-SSO fallback: the shared key travels in the agent definition
        return MCPTool(headers={AUTH_HEADER: settings.services_mcp_key}, **common)
    return None


SHARED_KB_NOTE = (
    "\n\n# Knowledge base status\n"
    "The search service quota did not allow a dedicated knowledge base for '{category}', so your knowledge base tool searches ALL product "
    "documents. Only use retrieved documents whose product family is '{category}'; ignore documents about other product families even if "
    "they look relevant. If nothing matches '{category}', reply with the 'no information in the knowledge base' sentence."
)
EMPTY_KB_NOTE = (
    "\n\n# Knowledge base status\n"
    "No documents have been uploaded for the '{category}' product family yet, so you have NO knowledge base tool and no "
    "facts to work from. For ANY product question, reply only with the 'no information in the knowledge base' sentence "
    "from the grounding rules (in the user's language) and add one short line that documents for {category} can be "
    "uploaded in Studio. Never answer from memory."
)


def desired_definition(settings: Settings, spec: SkillSpec, base_body: str, kb_owner: Optional[SkillSpec] = None, *, shared_by_quota: bool = False) -> PromptAgentDefinition:
    """Skills whose category has documents get their own KB tool; `general` (no filter) always has one.
    A skill without documents gets NO tool and must say its knowledge base is empty (keeps the POC honest).
    `shared_by_quota`: documents exist but the search tier has no knowledge-source quota left -> use the shared base with a scoping note."""
    instructions = compose_instructions(base_body, spec, RL.prompt_block_for_skill(RL.active_pack(settings), spec))
    tools = [kb_tool(settings, spec)]
    if kb_owner is not None and kb_owner.id != spec.id:
        if shared_by_quota:
            instructions += SHARED_KB_NOTE.format(category=spec.product_category)
            tools = [kb_tool(settings, spec, kb_owner)]
        elif not spec.tools:
            instructions += EMPTY_KB_NOTE.format(category=spec.product_category)
            tools = []
        else:  # a tool-only skill (live lookups, no documents): it must use its tool, not refuse
            instructions += TOOL_ONLY_NOTE.format(tools=", ".join(spec.tools))
            tools = []
    live = services_tool(settings, spec)  # live lookups are independent of whether the skill has documents
    if live is not None:
        tools = tools + [live]
    return PromptAgentDefinition(
        model=spec.model or settings.default_chat_model,
        instructions=instructions,
        tools=tools,
    )


def plan_kb_owners(settings: Settings, skills: dict[str, SkillSpec], facets: Optional[dict[str, int]] = None) -> dict[str, SkillSpec]:
    """Which skill's knowledge base each skill uses: its own when its category has documents, else the shared one."""
    shared = KB.shared_kb_spec(skills)
    owners: dict[str, SkillSpec] = {}
    for spec in skills.values():
        if KB.category_has_documents(settings, spec, facets):
            owners[spec.id] = spec
        elif shared is not None:
            owners[spec.id] = shared
        else:
            owners[spec.id] = spec
    return owners


def synced_kb_owners(settings: Settings, skills: dict[str, SkillSpec]) -> dict[str, SkillSpec]:
    """Which knowledge base each skill's agent really got, for callers that must query the same one it retrieves from.

    `plan_kb_owners` answers what the NEXT sync would do; the sync state records what the last one actually did (a
    skill with documents still lands on the shared base when the search tier has no knowledge-source quota left).
    Falls back to the plan when there is no state yet or it cannot be read."""
    owners = plan_kb_owners(settings, skills)
    try:
        agents = (_load_state(settings) or {}).get("agents") or {}
    except Exception:  # noqa: BLE001 - the plan is a fine fallback
        return owners
    by_kb = {s.kb_name: s for s in skills.values()}
    for spec in skills.values():
        kb = str((agents.get(spec.agent_name) or {}).get("kb") or "")
        if kb and kb != spec.kb_name and kb in by_kb:
            owners[spec.id] = by_kb[kb]
    return owners


def spec_hash(definition: PromptAgentDefinition) -> str:
    data = definition.as_dict() if hasattr(definition, "as_dict") else dict(definition)
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------- remote state ----------
def remote_latest(client: AIProjectClient, agent_name: str):
    try:
        details = client.agents.get(agent_name)
    except ResourceNotFoundError:
        return None
    return details.versions.latest if details.versions else None


def remote_hash(client: AIProjectClient, agent_name: str) -> Optional[str]:
    latest = remote_latest(client, agent_name)
    if latest is None:
        return None
    return (latest.metadata or {}).get("spec_hash")


def _load_state(settings: Settings) -> dict:
    p = settings.state_dir / STATE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_state(settings: Settings, state: dict) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- sync ----------
def ensure_agent(client: AIProjectClient, name: str, definition: PromptAgentDefinition, metadata: dict[str, str], description: str, *, keep: int = 0) -> tuple[str, str]:
    """Create a new version only when the spec hash differs. Returns (action, version)."""
    h = spec_hash(definition)
    latest = remote_latest(client, name)
    if latest is not None and (latest.metadata or {}).get("spec_hash") == h:
        return "unchanged", str(latest.version)
    created = client.agents.create_version(agent_name=name, definition=definition, metadata={**metadata, "spec_hash": h}, description=description)
    if keep and keep > 0:
        try:
            versions = list(client.agents.list_versions(agent_name=name))
            versions.sort(key=lambda v: int(str(v.version)) if str(v.version).isdigit() else 0, reverse=True)
            for old in versions[keep:]:
                client.agents.delete_version(agent_name=name, agent_version=str(old.version), force=True)
        except Exception:  # noqa: BLE001 - pruning old versions is best effort
            pass
    return ("created" if latest is None else "updated"), str(created.version)


def sync_skills(
    settings: Settings,
    skills: dict[str, SkillSpec],
    base_body: str,
    *,
    only: Optional[str] = None,
    prune: bool = False,
    keep: int = 0,
    register_native: bool = False,
    skip_kb: bool = False,
    skip_connections: bool = False,
    log: Log = print,
) -> SyncReport:
    settings.require("project_endpoint", "search_endpoint", "search_admin_key")
    report = SyncReport()
    client = project_client(settings)
    sic = index_client(settings)
    cred = credential()
    state = _load_state(settings)
    state.setdefault("agents", {})
    owners = plan_kb_owners(settings, skills)
    shared = KB.shared_kb_spec(skills)
    ordered = sorted(skills.values(), key=lambda s: (0 if shared and s.id == shared.id else 1, s.id))

    targets = [s for s in ordered if not only or s.id == only or (shared and s.id == shared.id and owners.get(only) is shared)]
    progress(log, "sync", 0, len(targets) + 1, message="")
    for spec in ordered:
        if only and spec.id != only and not (shared and spec.id == shared.id and owners.get(only) is shared):
            continue
        progress(log, "sync", targets.index(spec), len(targets) + 1, message=spec.id)
        owner = owners[spec.id]
        row = SyncRow(skill_id=spec.id, agent=spec.agent_name)
        shared_by_quota = False
        try:
            if owner.id == spec.id:
                if not skip_kb:
                    try:
                        row.knowledge_source, row.knowledge_base = KB.ensure_knowledge_objects(settings, spec, client=sic)
                        log(f"[{spec.id}] knowledge source {row.knowledge_source} + knowledge base {row.knowledge_base} ok")
                    except Exception as e:  # noqa: BLE001
                        if "quota" not in str(e).lower() or shared is None or shared.id == spec.id:
                            raise
                        shared_by_quota = True
                        owner = shared
                        row.knowledge_base, row.connection = f"{shared.kb_name} (shared: quota)", shared.connection_name
                        row.note = "knowledge-source quota exceeded (Free tier = 3); agent uses the shared base with a scoping note"
                        log(f"[{spec.id}] {row.note}")
                if not shared_by_quota and settings.kb_mcp_auth != "apikey" and not skip_connections:
                    CONN.ensure_kb_connection(settings, spec, cred)
                    row.connection = spec.connection_name
                    log(f"[{spec.id}] project connection {row.connection} ok")
            else:
                row.knowledge_base, row.connection = "(none: no documents yet)", ""
                row.note = f"no documents for '{spec.product_category}' yet; agent has no retrieval tool"
                log(f"[{spec.id}] {row.note}")
                if not skip_kb:
                    KB.delete_knowledge_objects(settings, spec, client=sic)
                if settings.kb_mcp_auth != "apikey" and not skip_connections:
                    CONN.delete_connection(settings, spec.connection_name, cred)
            if spec.tools and settings.services_mcp_audience and not skip_connections:
                CONN.ensure_services_connection(settings, cred)  # idempotent PUT; the agent calls us as the project MI
                log(f"[{spec.id}] live-service connection {CONN.SERVICES_CONNECTION} ok ({', '.join(spec.tools)})")
            definition = desired_definition(settings, spec, base_body, owner, shared_by_quota=shared_by_quota)
            row.action, row.version = ensure_agent(
                client, spec.agent_name, definition, {"source": SOURCE_TAG, "skill_id": spec.id, "skill_version": str(spec.version)},
                f"bankrag skill '{spec.name}' ({spec.product_category})", keep=keep,
            )
            state["agents"][spec.agent_name] = {"skill_id": spec.id, "version": row.version, "spec_hash": spec_hash(definition), "kb": owner.kb_name}
            log(f"[{spec.id}] agent {spec.agent_name}: {row.action} (version {row.version})")
        except Exception as e:  # noqa: BLE001
            row.action = "error"
            row.note = f"{type(e).__name__}: {str(e)[:300]}"
            log(f"[{spec.id}] ERROR {row.note}")
        report.rows.append(row)

    # ---- native Foundry: skill registry + toolbox, A2A endpoints + concierge (all hash-guarded) ----
    from . import foundry_native as FN

    synced_ids = [s.id for s in targets]
    if settings.foundry_native_skills or register_native:
        for spec in targets:
            try:
                action, version = FN.publish_skill(client, spec, base_body, state, log, RL.prompt_block_for_skill(RL.active_pack(settings), spec))
                report.rows.append(SyncRow(skill_id=f"{spec.id} (registry)", agent=FN.registry_name(spec.id), action=action if action != "published" else "updated", version=version))
            except Exception as e:  # noqa: BLE001
                report.rows.append(SyncRow(skill_id=f"{spec.id} (registry)", agent=FN.registry_name(spec.id), action="error", note=f"{type(e).__name__}: {str(e)[:200]}"))
                log(f"[{spec.id}] registry ERROR {type(e).__name__}: {str(e)[:200]}")
        try:
            v = FN.ensure_skill_toolbox(client, sorted(skills), state, log)
            report.rows.append(SyncRow(skill_id="(skill toolbox)", agent=FN.SKILL_TOOLBOX, action="ok", version=v or ""))
        except Exception as e:  # noqa: BLE001
            report.rows.append(SyncRow(skill_id="(skill toolbox)", agent=FN.SKILL_TOOLBOX, action="error", note=f"{type(e).__name__}: {str(e)[:200]}"))
    connection_ids: dict[str, str] = dict(state.get("a2a_connections", {}))
    progress(log, "sync", len(targets), len(targets) + 1, message="A2A handoff")
    for spec in targets:
        try:
            a = FN.enable_a2a(client, spec, state, log)
            if spec.id not in connection_ids:
                connection_ids[spec.id] = FN.ensure_a2a_connection(settings, spec, cred)
                log(f"  A2A connection {FN.a2a_connection_name(spec.id)} ready")
            report.rows.append(SyncRow(skill_id=f"{spec.id} (A2A)", agent=spec.agent_name, connection=FN.a2a_connection_name(spec.id), action="unchanged" if a == "unchanged" else "updated", note="A2A endpoint + connection"))
        except Exception as e:  # noqa: BLE001
            report.rows.append(SyncRow(skill_id=f"{spec.id} (A2A)", agent=spec.agent_name, action="error", note=f"{type(e).__name__}: {str(e)[:200]}"))
            log(f"[{spec.id}] A2A ERROR {type(e).__name__}: {str(e)[:200]}")
    state["a2a_connections"] = connection_ids
    if connection_ids:
        row = SyncRow(skill_id="(concierge)", agent=FN.CONCIERGE_AGENT)
        try:
            row.action, row.version = ensure_agent(client, FN.CONCIERGE_AGENT, FN.concierge_definition(settings, skills, connection_ids), {"source": SOURCE_TAG, "skill_id": "concierge"},
                                                   "bankrag concierge: hands each question to a specialist agent over A2A", keep=keep)
            log(f"[concierge] agent {FN.CONCIERGE_AGENT}: {row.action} (version {row.version})")
        except Exception as e:  # noqa: BLE001
            row.action, row.note = "error", f"{type(e).__name__}: {str(e)[:300]}"
            log(f"[concierge] ERROR {row.note}")
        report.rows.append(row)

    if True:  # the router enum lists every skill id, so reconcile it on every sync (cheap: hash-guarded)
        row = SyncRow(skill_id="(router)", agent=ROUTER_AGENT)
        progress(log, "sync", len(targets), len(targets) + 1, message="router")
        try:
            row.action, row.version = ensure_agent(
                client, ROUTER_AGENT, build_router_definition(settings, skills), {"source": SOURCE_TAG, "skill_id": "router"},
                "bankrag router: picks the product skill for a user message", keep=keep,
            )
            log(f"[router] agent {ROUTER_AGENT}: {row.action} (version {row.version})")
        except Exception as e:  # noqa: BLE001
            row.action, row.note = "error", f"{type(e).__name__}: {str(e)[:300]}"
            log(f"[router] ERROR {row.note}")
        report.rows.append(row)

    if prune:
        for details in client.agents.list():
            latest = details.versions.latest if details.versions else None
            meta = (latest.metadata or {}) if latest else {}
            if meta.get("source") != SOURCE_TAG or meta.get("skill_id") in ("router", None):
                continue
            sid = meta.get("skill_id")
            if sid in skills:
                continue
            log(f"[prune] deleting agent {details.name} (skill '{sid}' no longer exists)")
            client.agents.delete(details.name, force=True)
            ghost = SkillSpec(id=sid, name=sid, description="", product_category=sid)
            KB.delete_knowledge_objects(settings, ghost, client=sic)
            if settings.kb_mcp_auth != "apikey":
                CONN.delete_connection(settings, ghost.connection_name, cred)
            state["agents"].pop(details.name, None)
            report.rows.append(SyncRow(skill_id=sid, agent=details.name, action="pruned"))

    _save_state(settings, state)
    return report


def status(settings: Settings, skills: dict[str, SkillSpec], base_body: str) -> list[dict]:
    """Local vs remote hash per skill (for the Skills panel). Never raises on auth problems."""
    rows: list[dict] = []
    client = None
    auth_error = ""
    try:
        credential().get_token("https://ai.azure.com/.default")  # one probe instead of one failure per skill
        client = project_client(settings)
    except Exception as e:  # noqa: BLE001
        auth_error = f"error: {type(e).__name__}"
    owners = plan_kb_owners(settings, skills)
    native = _load_state(settings)
    latest_by_name: dict = {}
    list_error = ""
    if client is not None:
        try:
            for details in client.agents.list():
                latest_by_name[details.name] = details.versions.latest if details.versions else None
        except Exception as e:  # noqa: BLE001
            list_error = f"error: {type(e).__name__}"
    for spec in sorted(skills.values(), key=lambda s: s.id):
        local = spec_hash(desired_definition(settings, spec, base_body, owners[spec.id]))
        remote, version, state = None, "", auth_error or list_error or "unknown"
        if client is not None and not list_error:
            latest = latest_by_name.get(spec.agent_name)
            if latest is None:
                state = "missing"
            else:
                remote = (latest.metadata or {}).get("spec_hash")
                version = str(latest.version)
                state = "in-sync" if remote == local else "outdated"
        rows.append(
            {
                "id": spec.id, "name": spec.name, "product_category": spec.product_category, "model": spec.model or settings.default_chat_model,
                "top_k": spec.top_k, "filter": spec.effective_filter, "agent": spec.agent_name, "knowledge_base": owners[spec.id].kb_name if owners[spec.id].id == spec.id else "(none: upload documents + sync)",
                "local_hash": local, "remote_hash": remote, "version": version, "state": state, "path": str(spec.path) if spec.path else "",
                "registry_version": native.get("registry", {}).get(spec.id, {}).get("version", ""), "a2a": spec.id in native.get("a2a_connections", {}),
            }
        )
    return rows
