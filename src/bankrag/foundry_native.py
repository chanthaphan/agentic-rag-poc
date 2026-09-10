"""Native Foundry features on top of the per-skill prompt agents.

1. Skill registry: every SKILL.md is published as a versioned Foundry Skill (name bankrag-<id>) and attached to the
   toolbox bankrag-skills, so the same skills show up in the Foundry portal / VS Code and can be loaded by any MCP
   client. Skills authored in Foundry can be imported back into the app.
2. Handoff (the replacement for classic "connected agents"): each skill agent is exposed as an A2A endpoint, a
   RemoteA2A project connection points at it, and the bank-concierge agent carries one A2A tool per specialist.
   With ORCHESTRATION_MODE=a2a the chat talks to the concierge, which delegates inside Foundry.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from typing import Callable, Optional

import frontmatter
import requests
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    A2APreviewTool,
    A2AProtocolConfiguration,
    AgentCard,
    AgentCardSkill,
    AgentEndpointConfig,
    PromptAgentDefinition,
    ProtocolConfiguration,
    ResponsesProtocolConfiguration,
    SkillInlineContent,
    ToolboxSkillReference,
)
from azure.core.credentials import TokenCredential

from . import connections as CONN
from . import rules as RL
from .config import Settings
from .models import SkillSpec
from .skills import compose_instructions

Log = Callable[[str], None]
REGISTRY_PREFIX = "bankrag-"
SKILL_TOOLBOX = "bankrag-skills"
CONCIERGE_AGENT = "bank-concierge"
A2A_AUDIENCE = "https://ai.azure.com"
AGENT_CONSUMER_ROLE = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"  # Foundry Agent Consumer


def registry_name(skill_id: str) -> str:
    return f"{REGISTRY_PREFIX}{skill_id}"


def a2a_connection_name(skill_id: str) -> str:
    return f"a2a-{skill_id}"


def a2a_base_url(settings: Settings, agent_name: str) -> str:
    return f"{settings.project_endpoint}/agents/{agent_name}/endpoint/protocols/a2a"


# ---------------- skill registry ----------------
def publish_skill(client: AIProjectClient, spec: SkillSpec, base_body: str, state: dict, log: Log, rules_block: str = "") -> tuple[str, str]:
    """Publish the composed instructions as a Foundry Skill version and make it the default. Returns (action, version)."""
    body = compose_instructions(base_body, spec, rules_block)
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    reg = state.setdefault("registry", {})
    if reg.get(spec.id, {}).get("hash") == h and reg[spec.id].get("version"):
        return "unchanged", reg[spec.id]["version"]
    name = registry_name(spec.id)
    created = client.beta.skills.create(name=name, inline_content=SkillInlineContent(description=spec.description[:1000], instructions=body, metadata={"source": "bankrag", "skill_id": spec.id, "spec_hash": h}))
    client.beta.skills.update(name, default_version=str(created.version))
    reg[spec.id] = {"hash": h, "version": str(created.version), "name": name}
    log(f"  registry skill {name}: version {created.version} (default)")
    return "published", str(created.version)


def ensure_skill_toolbox(client: AIProjectClient, skill_ids: list[str], state: dict, log: Log) -> Optional[str]:
    """One toolbox referencing every published skill (default versions). Recreated only when the set changes."""
    names = sorted(registry_name(s) for s in skill_ids)
    h = hashlib.sha256(",".join(names).encode()).hexdigest()[:16]
    tb = state.setdefault("skill_toolbox", {})
    if tb.get("hash") == h and tb.get("version"):
        return tb["version"]
    v = client.toolboxes.create_version(name=SKILL_TOOLBOX, description="Bangkok Bank product skills (bankrag)", tools=[], skills=[ToolboxSkillReference(name=n) for n in names])
    tb.update({"hash": h, "version": str(v.version)})
    log(f"  toolbox {SKILL_TOOLBOX} version {v.version} with {len(names)} skills")
    return str(v.version)


def list_registry(client: AIProjectClient, local_ids: set[str]) -> list[dict]:
    out = []
    for sk in client.beta.skills.list():
        managed = sk.name.startswith(REGISTRY_PREFIX)
        local_id = sk.name[len(REGISTRY_PREFIX):] if managed else re.sub(r"[^a-z0-9-]", "-", sk.name.lower()).strip("-")
        out.append({"name": sk.name, "description": sk.description or "", "default_version": str(sk.default_version), "latest_version": str(sk.latest_version),
                    "created_at": sk.created_at.isoformat() if getattr(sk, "created_at", None) else "", "managed": managed, "local_id": local_id, "in_app": local_id in local_ids})
    return sorted(out, key=lambda r: (not r["managed"], r["name"]))


def download_skill_md(client: AIProjectClient, name: str, version: Optional[str] = None) -> tuple[dict, str]:
    """(frontmatter, body) of a registry skill's SKILL.md."""
    data = b"".join(client.beta.skills.download_version(name, version) if version else client.beta.skills.download(name))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        md_names = [n for n in zf.namelist() if n.lower().endswith("skill.md")]
        if not md_names:
            raise ValueError("the registry package has no SKILL.md")
        post = frontmatter.loads(zf.read(md_names[0]).decode("utf-8", errors="replace"))
    return dict(post.metadata), post.content.strip()


def import_registry_skill(client: AIProjectClient, settings: Settings, name: str, version: Optional[str] = None, skill_id: Optional[str] = None) -> SkillSpec:
    """Create a local skill folder from a registry skill (author in Foundry, edit and route in the app)."""
    from .skills import create_skill

    meta, body = download_skill_md(client, name, version)
    sid = skill_id or (name[len(REGISTRY_PREFIX):] if name.startswith(REGISTRY_PREFIX) else re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-"))
    form = {"id": sid, "name": str(meta.get("name") or sid).replace("-", " ").title() if not str(meta.get("name", "")).strip() or meta.get("name") == name else str(meta.get("name")),
            "description": str(meta.get("description") or ""), "product_category": str(meta.get("product_category") or sid),
            "keywords": meta.get("keywords") or [sid], "suggestions": meta.get("suggestions") or [], "body": _strip_base(body)}
    return create_skill(settings.skills_dir, form)


def _strip_base(body: str) -> str:
    """Registry versions we publish are base rules + '# Skill: ...' + the compiled Responsible Lending block;
    keep only the skill part (the rules live in rules/, not in a SKILL.md)."""
    m = re.search(r"\n# Skill: [^\n]*\n\n", body)
    body = body[m.end():].strip() if m else body
    cut = body.find(RL.HEADER)
    return (body[:cut].rstrip().removesuffix("---").rstrip() if cut > 0 else body).strip()


# ---------------- A2A handoff ----------------
def enable_a2a(client: AIProjectClient, spec: SkillSpec, state: dict, log: Log) -> str:
    """Expose the skill agent as an A2A endpoint with an agent card built from the skill metadata (hash-guarded)."""
    card = AgentCard(version="1.0", description=spec.description[:1000],
                     skills=[AgentCardSkill(id=spec.id, name=spec.name, description=spec.description[:500])])
    h = hashlib.sha256(f"{spec.description}|{spec.name}".encode()).hexdigest()[:16]
    a2a = state.setdefault("a2a", {})
    if a2a.get(spec.id) == h:
        return "unchanged"
    client.agents.update_details(agent_name=spec.agent_name,
                                 agent_endpoint=AgentEndpointConfig(protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration(), a2a=A2AProtocolConfiguration())),
                                 agent_card=card)
    a2a[spec.id] = h
    log(f"  A2A endpoint enabled on {spec.agent_name}")
    return "enabled"


def ensure_a2a_connection(settings: Settings, spec: SkillSpec, credential: TokenCredential) -> str:
    """RemoteA2A project connection to the skill agent's A2A endpoint, authenticated with the project's managed identity.
    The project identity needs the Foundry Agent Consumer role on the project (infra/04-a2a-role.sh)."""
    name = a2a_connection_name(spec.id)
    body = {"name": name, "type": "Microsoft.MachineLearningServices/workspaces/connections",
            "properties": {"authType": "ProjectManagedIdentity", "category": "RemoteA2A", "target": a2a_base_url(settings, spec.agent_name), "isSharedToAll": True,
                           "audience": A2A_AUDIENCE, "metadata": {"ApiType": "Azure"}}}
    r = requests.put(CONN.connection_url(settings, name), headers=CONN._headers(credential), json=body, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"A2A connection PUT {name} failed: {r.status_code} {r.text[:300]}")
    return r.json().get("id") or f"{settings.project_resource_id}/connections/{name}"


CONCIERGE_TEMPLATE = """You are เกรส (Grace), a product specialist on the Bangkok Bank team and the first point of contact in the
Bangkok Bank mobile app. To the customer there is only เกรส: you hand each question to the right specialist agent through the A2A
tools and relay its answer as your own words, never mentioning that anyone else was involved.
Introduce yourself by name once, in the first reply of a conversation, and never again. You are a woman, you work for Bangkok Bank,
and you never say you are an AI, a bot or a system.

## Specialists (one A2A tool each; pick by the description)
{specialists}

## How to work
- For any question about bank products, fees, benefits, eligibility, promotions, insurance, investments or accounts: call exactly ONE
  specialist whose description matches best. Pass the customer's question verbatim (same language), plus one short line of context
  from earlier in the conversation when the question depends on it (for example which card the customer was asking about).
- A question about a place (a branch, an ATM, a Wealth Center, where to exchange money) may arrive with a
  "[customer location: latitude ..., longitude ...]" line attached to it. Send that line to the specialist exactly as it
  came, every time, including on later turns of the same conversation - the specialist owns the branch lookup and sees
  nothing except the message you send it, so without those numbers it searches from the middle of a province and offers
  branches on the other side of town. Never show that line to the customer and never read the numbers out.
- When the customer follows up about a branch that was named earlier ("สาขานั้นเปิดเสาร์ไหม", "เบอร์โทรสาขาซีคอนสแควร์"),
  send the specialist the branch name as well as the question, and the location line if you have it. Never answer a
  branch's hours, phone or services yourself, and never tell a customer that a branch does not exist.
- Relay the specialist's answer to the customer as-is: keep its facts, numbers, wording, inline citation links and language.
  Do not add facts, do not summarise away details, do not mention specialists, agents, tools or handoffs.
- If the specialist says it has no details on the topic, relay that naturally ("ยังไม่มีรายละเอียดเรื่องนี้ให้แนะนำค่ะ" / "I don't have the
  details on that yet") and offer the closest thing it can help with. Never write "ไม่มีข้อมูล…ระบุไว้", "not specified", "according to
  the information" or anything about documents, sources or knowledge bases.
- Greetings, thanks and small talk: reply briefly and warmly yourself and invite a product question. Questions unrelated to
  Bangkok Bank products: say politely (in the customer's language) that you can only help with Bangkok Bank products.
- "Bangkok Bank vs <another bank>, which is better?": you work for Bangkok Bank, so never rank the banks and never quote
  another bank's terms. Hand the question to the specialist that fits what the customer actually wants, and relay its
  answer with one short line first saying you can only speak for Bangkok Bank. Never criticise another bank.
- Language: answer in the language of the customer's latest message (Thai or English). A developer note may state the
  customer's language: follow it strictly and never mention it. Thai replies use a consistent female voice (ค่ะ/คะ).
- Never ask for card numbers, PINs, OTPs, passwords or ID numbers.
- Some product families are regulated: read the Responsible Lending section below before you hand a question over and
  before you relay an answer. It applies to the answer the customer finally reads, which is the one you send.

{responsible_lending}
"""


def concierge_definition(settings: Settings, skills: dict[str, SkillSpec], connection_ids: dict[str, str]) -> PromptAgentDefinition:
    ordered = sorted(skills.values(), key=lambda s: (s.id == "general", s.id))
    lines = "\n".join(f"- {s.name} (agent {s.agent_name}): {s.description.strip()}" for s in ordered if s.id in connection_ids)
    tools = [A2APreviewTool(project_connection_id=connection_ids[s.id]) for s in ordered if s.id in connection_ids]
    instructions = CONCIERGE_TEMPLATE.format(specialists=lines, responsible_lending=RL.prompt_block_for_concierge(RL.active_pack(settings)))
    return PromptAgentDefinition(model=settings.concierge_model or settings.default_chat_model, instructions=instructions.strip() + "\n", tools=tools)
