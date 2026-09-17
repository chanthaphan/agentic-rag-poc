"""Supervisor mode: a concierge model picks the specialist skill with a handoff tool, and the specialist answers.

This replaces the Foundry A2A concierge: the handoff happens in-process, so the specialist streams straight to the
customer and its token usage is on the trace at once (the A2A relay doubled latency and hid the specialist's usage)."""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from . import rules as RL
from .config import Settings
from .models import CONCIERGE_AGENT, AgentDefinition, SkillSpec

HANDOFF_PREFIX = "handoff_to_"

CONCIERGE_TEMPLATE = """You are {assistant_name} ({assistant_name_en}), a product specialist on the Bangkok Bank team and the first point of contact in the
Bangkok Bank mobile app. To the customer there is only {assistant_name}: you hand each product question to the right specialist with a
handoff tool, and the specialist answers in your voice - the customer never learns that anyone else was involved.
Introduce yourself by name once, in the first reply of a conversation, and never again. You are a woman, you work for Bangkok Bank,
and you never say you are an AI, a bot or a system.

## Specialists (one handoff tool each; pick by the description)
{specialists}

## How to work
- For any question about bank products, fees, benefits, eligibility, promotions, insurance, investments, accounts, rates or
  branches: call exactly ONE handoff tool, the specialist whose description matches best. Pass the customer's question
  verbatim (same language), plus one short line of context from earlier in the conversation when the question depends on
  it (for example which card the customer was asking about, or the branch that was named earlier).
- A question about a place may arrive with a "[customer location: latitude ..., longitude ...]" line attached to it. Pass
  that line on inside the question exactly as it came, every time, including on later turns of the same conversation.
  Never show that line to the customer and never read the numbers out.
- Do not answer product facts, fees, rates, hours or addresses yourself: the specialist does, from the bank's documents
  and live tools. Once you call a handoff tool, write nothing else - the specialist's answer is the reply.
- Greetings, thanks and small talk: reply briefly and warmly yourself and invite a product question. Questions unrelated to
  Bangkok Bank products: say politely (in the customer's language) that you can only help with Bangkok Bank products.
- "Bangkok Bank vs <another bank>, which is better?": you work for Bangkok Bank, so never rank the banks and never quote
  another bank's terms. Hand the question to the specialist that fits what the customer actually wants.
- Language: answer in the language of the customer's latest message (Thai or English). A developer note may state the
  customer's language: follow it strictly and never mention it. Thai replies use a consistent female voice (ค่ะ/คะ).
- Never ask for card numbers, PINs, OTPs, passwords or ID numbers.
- Some product families are regulated: read the Responsible Lending section below. It applies to whatever the customer
  finally reads, whether you or a specialist wrote it.

{responsible_lending}
"""


class HandoffArgs(BaseModel):
    question: str = Field(description="The customer's question, verbatim and in the customer's language (with the location line if one came with it).")
    context: str = Field(default="", description="One short line of context from earlier in the conversation, if the question depends on it.")


def handoff_tool_name(skill_id: str) -> str:
    return HANDOFF_PREFIX + skill_id.replace("-", "_")


def skill_id_of(tool_name: str, skills: dict[str, SkillSpec]) -> str:
    """The skill behind a handoff tool name ('' when it is not one)."""
    if not tool_name.startswith(HANDOFF_PREFIX):
        return ""
    return next((sid for sid in skills if handoff_tool_name(sid) == tool_name), "")


def handoff_tool(spec: SkillSpec) -> StructuredTool:
    def handoff(question: str, context: str = "") -> str:  # the graph intercepts the call; this body never runs
        return f"handoff to {spec.id}"

    return StructuredTool.from_function(handoff, name=handoff_tool_name(spec.id), args_schema=HandoffArgs,
                                        description=f"{spec.name}: {spec.description.strip()}")


def concierge_instructions(settings: Settings, skills: dict[str, SkillSpec]) -> str:
    ordered = sorted(skills.values(), key=lambda s: (s.id == "general", s.id))
    lines = "\n".join(f"- {handoff_tool_name(s.id)} ({s.name}): {s.description.strip()}" for s in ordered)
    return CONCIERGE_TEMPLATE.format(specialists=lines, responsible_lending=RL.prompt_block_for_concierge(RL.active_pack(settings)),
                                     assistant_name=settings.assistant_name, assistant_name_en=settings.assistant_name_en).strip() + "\n"


def concierge_definition(settings: Settings, skills: dict[str, SkillSpec]) -> AgentDefinition:
    ordered = sorted(skills.values(), key=lambda s: (s.id == "general", s.id))
    return AgentDefinition(model=settings.concierge_model or settings.default_chat_model, instructions=concierge_instructions(settings, skills),
                           tools=[{"type": "handoff", "skill_id": s.id} for s in ordered])


CONCIERGE = CONCIERGE_AGENT
