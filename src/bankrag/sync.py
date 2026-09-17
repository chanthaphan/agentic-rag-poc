"""Publish skills/<id>/SKILL.md: knowledge source + knowledge base in Azure AI Search, and a versioned agent definition
in the local registry (`agent_versions`). Idempotent: a new version is created only when the definition's hash changed."""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional

from . import agent_versions as AV
from . import knowledge_base as KB
from . import rules as RL
from .ingest.progress import report as progress
from .config import Settings
from .models import CONCIERGE_AGENT, ROUTER_AGENT, AgentDefinition, SkillSpec, SyncReport, SyncRow
from .router import build_router_definition
from .search_index import index_client
from .skills import compose_instructions

Log = Callable[[str], None]
SOURCE_TAG = "bankrag"
STATE_FILE = "sync_state.json"
LEGACY_STATE_FILE = "foundry_state.json"
KB_TOOL = "knowledge_base_retrieve"


# ---------- definitions ----------
def kb_tool_ref(settings: Settings, spec: SkillSpec, kb_owner: Optional[SkillSpec] = None) -> dict:
    """The knowledge-base MCP tool of a skill; `kb_owner` overrides which skill's knowledge base is used (shared KB).
    The auth mode is part of the definition; the key itself never is."""
    owner = kb_owner or spec
    return {
        "type": "mcp",
        "server_label": "knowledge-base",
        "server_url": settings.kb_mcp_url(owner.kb_name),
        "kb_name": owner.kb_name,
        "allowed_tools": [KB_TOOL],
        "auth": "apikey" if settings.kb_mcp_auth == "apikey" else "identity",
    }


TOOL_ONLY_NOTE = (
    "\n\n# Knowledge base status\n"
    "You have no product documents, and you do not need any: your answers come from your live tool(s) ({tools}). "
    "Call the tool for every question in your scope and answer from what it returns. Never say your knowledge base is "
    "empty, and never answer a live value from memory or from earlier in the conversation."
)


def services_tool_refs(settings: Settings, spec: SkillSpec) -> list[dict]:
    """The live-service tools a skill declares in its frontmatter (`tools: [fx_rate]`), called in-process."""
    from .tools import SERVICE_TOOL_NAMES

    return [{"type": "function", "name": t} for t in spec.tools if t in SERVICE_TOOL_NAMES]


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


def desired_definition(settings: Settings, spec: SkillSpec, base_body: str, kb_owner: Optional[SkillSpec] = None, *, shared_by_quota: bool = False) -> AgentDefinition:
    """Skills whose category has documents get their own KB tool; `general` (no filter) always has one.
    A skill without documents gets NO tool and must say its knowledge base is empty (keeps the POC honest).
    `shared_by_quota`: documents exist but the search tier has no knowledge-source quota left -> use the shared base with a scoping note."""
    instructions = compose_instructions(base_body, spec, RL.prompt_block_for_skill(RL.active_pack(settings), spec))
    live = services_tool_refs(settings, spec)
    tools = [kb_tool_ref(settings, spec)]
    if kb_owner is not None and kb_owner.id != spec.id:
        if shared_by_quota:
            instructions += SHARED_KB_NOTE.format(category=spec.product_category)
            tools = [kb_tool_ref(settings, spec, kb_owner)]
        elif live:  # a tool-only skill (live lookups, no documents): it must use its tool, not refuse
            instructions += TOOL_ONLY_NOTE.format(tools=", ".join(t["name"] for t in live))
            tools = []
        else:
            instructions += EMPTY_KB_NOTE.format(category=spec.product_category)
            tools = []
    return AgentDefinition(model=spec.model or settings.default_chat_model, instructions=instructions, tools=tools + live)


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


def spec_hash(definition: AgentDefinition) -> str:
    data = definition.as_dict() if hasattr(definition, "as_dict") else dict(definition)
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------- local state (which KB each agent got) ----------
def _load_state(settings: Settings) -> dict:
    p = settings.state_dir / STATE_FILE
    if not p.exists():
        p = settings.state_dir / LEGACY_STATE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_state(settings: Settings, state: dict) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- registry ----------
def ensure_agent(settings: Settings, name: str, definition: AgentDefinition, metadata: dict[str, str], description: str, *, keep: int = 0) -> tuple[str, str]:
    """Publish a new version only when the spec hash differs. Returns (action, version)."""
    h = spec_hash(definition)
    latest = AV.latest(settings, name)
    if latest is not None and latest.get("spec_hash") == h:
        return "unchanged", str(latest["version"])
    version = AV.create(settings, name, definition, {**metadata, "spec_hash": h}, description)
    if keep and keep > 0:
        AV.prune(settings, name, keep)
    return ("created" if latest is None else "updated"), str(version)


def published_definition(settings: Settings, agent: str) -> Optional[tuple[AgentDefinition, str]]:
    row = AV.latest(settings, agent)
    if row is None:
        return None
    return AV.to_definition(row), str(row["version"])


def runtime_definition(settings: Settings, spec: SkillSpec, base_body: str, owners: dict[str, SkillSpec]) -> tuple[AgentDefinition, str]:
    """The definition the chat runs with: the latest published version, or the live composition before the first sync."""
    pub = published_definition(settings, spec.agent_name)
    if pub is not None:
        return pub
    return desired_definition(settings, spec, base_body, owners.get(spec.id, spec)), ""


# ---------- sync ----------
def sync_skills(
    settings: Settings,
    skills: dict[str, SkillSpec],
    base_body: str,
    *,
    only: Optional[str] = None,
    prune: bool = False,
    keep: int = 0,
    skip_kb: bool = False,
    log: Log = print,
) -> SyncReport:
    from .supervisor import concierge_definition

    if not skip_kb:
        settings.require("search_endpoint", "search_admin_key")
    report = SyncReport()
    sic = index_client(settings) if not skip_kb else None
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
                        row.knowledge_base = f"{shared.kb_name} (shared: quota)"
                        row.note = "knowledge-source quota exceeded (Free tier = 3); agent uses the shared base with a scoping note"
                        log(f"[{spec.id}] {row.note}")
                else:
                    row.knowledge_base = owner.kb_name
            else:
                row.knowledge_base = "(none: no documents yet)"
                row.note = f"no documents for '{spec.product_category}' yet; agent has no retrieval tool"
                log(f"[{spec.id}] {row.note}")
                if not skip_kb:
                    KB.delete_knowledge_objects(settings, spec, client=sic)
            definition = desired_definition(settings, spec, base_body, owner, shared_by_quota=shared_by_quota)
            row.action, row.version = ensure_agent(
                settings, spec.agent_name, definition, {"source": SOURCE_TAG, "skill_id": spec.id, "skill_version": str(spec.version)},
                f"bankrag skill '{spec.name}' ({spec.product_category})", keep=keep,
            )
            state["agents"][spec.agent_name] = {"skill_id": spec.id, "version": row.version, "spec_hash": spec_hash(definition), "kb": owner.kb_name}
            log(f"[{spec.id}] agent {spec.agent_name}: {row.action} (version {row.version})")
        except Exception as e:  # noqa: BLE001
            row.action = "error"
            row.note = f"{type(e).__name__}: {str(e)[:300]}"
            log(f"[{spec.id}] ERROR {row.note}")
        report.rows.append(row)

    # the concierge (supervisor mode) and the router list every skill, so reconcile them on every sync (hash-guarded)
    for name, build, desc in (
        (CONCIERGE_AGENT, lambda: concierge_definition(settings, skills), "bankrag concierge: hands each question to a specialist skill"),
        (ROUTER_AGENT, lambda: build_router_definition(settings, skills), "bankrag router: picks the product skill for a user message"),
    ):
        row = SyncRow(skill_id=f"({name[5:]})", agent=name)
        progress(log, "sync", len(targets), len(targets) + 1, message=name[5:])
        try:
            row.action, row.version = ensure_agent(settings, name, build(), {"source": SOURCE_TAG, "skill_id": name[5:]}, desc, keep=keep)
            log(f"[{name[5:]}] agent {name}: {row.action} (version {row.version})")
        except Exception as e:  # noqa: BLE001
            row.action, row.note = "error", f"{type(e).__name__}: {str(e)[:300]}"
            log(f"[{name[5:]}] ERROR {row.note}")
        report.rows.append(row)

    if prune:
        for details in AV.list_agents(settings):
            meta = details.get("metadata") or {}
            sid = meta.get("skill_id")
            if meta.get("source") != SOURCE_TAG or sid in ("router", "concierge", None) or sid in skills:
                continue
            log(f"[prune] deleting agent {details['agent']} (skill '{sid}' no longer exists)")
            AV.delete_agent(settings, details["agent"])
            if not skip_kb:
                KB.delete_knowledge_objects(settings, SkillSpec(id=sid, name=sid, description="", product_category=sid), client=sic)
            state["agents"].pop(details["agent"], None)
            report.rows.append(SyncRow(skill_id=sid, agent=details["agent"], action="pruned"))

    _save_state(settings, state)
    return report


def status(settings: Settings, skills: dict[str, SkillSpec], base_body: str) -> list[dict]:
    """Local vs published hash per skill (for the Skills panel)."""
    rows: list[dict] = []
    owners = plan_kb_owners(settings, skills)
    for spec in sorted(skills.values(), key=lambda s: s.id):
        local = spec_hash(desired_definition(settings, spec, base_body, owners[spec.id]))
        latest = AV.latest(settings, spec.agent_name)
        if latest is None:
            remote, version, state = None, "", "missing"
        else:
            remote, version = latest.get("spec_hash"), str(latest["version"])
            state = "in-sync" if remote == local else "outdated"
        rows.append(
            {
                "id": spec.id, "name": spec.name, "product_category": spec.product_category, "model": spec.model or settings.default_chat_model,
                "top_k": spec.top_k, "filter": spec.effective_filter, "agent": spec.agent_name,
                "knowledge_base": owners[spec.id].kb_name if owners[spec.id].id == spec.id else "(none: upload documents + sync)",
                "local_hash": local, "remote_hash": remote, "version": version, "state": state, "path": str(spec.path) if spec.path else "",
            }
        )
    return rows
