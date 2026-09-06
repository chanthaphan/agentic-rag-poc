"""Skill router: a Foundry prompt agent with strict JSON output, plus a keyword fallback."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from azure.ai.projects.models import PromptAgentDefinition, PromptAgentDefinitionTextOptions, TextResponseFormatJsonSchema

from .config import Settings
from .models import ROUTER_AGENT, RouteDecision, SkillSpec

OFFTOPIC = "offtopic"


def route_schema(skills: dict[str, SkillSpec]) -> dict[str, Any]:
    ids = sorted(skills) + [OFFTOPIC]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "skill_id": {"type": "string", "enum": ids},
            "confidence": {"type": "number"},
            "language": {"type": "string", "enum": ["th", "en"]},
            "reason": {"type": "string"},
        },
        "required": ["skill_id", "confidence", "language", "reason"],
    }


def router_instructions(skills: dict[str, SkillSpec]) -> str:
    catalog = []
    for s in sorted(skills.values(), key=lambda x: x.id):
        kw = ", ".join(s.keywords[:25])
        catalog.append(f"- id: {s.id}\n  name: {s.name}\n  covers: {s.description}\n  keywords: {kw}")
    general = "general" if "general" in skills else sorted(skills)[0]
    return (
        "You are a routing classifier for the Bangkok Bank product assistant. Read the user's latest message "
        "(plus recent turns and the previous skill) and pick exactly ONE skill id from the catalog.\n\n"
        "Catalog:\n" + "\n".join(catalog) + "\n\n"
        "Rules:\n"
        "1. Choose the skill whose products the user is asking about. Thai and English are both common; product names may mix scripts.\n"
        f"2. Follow-up questions, pronouns (มัน, อันนี้, this one) and short replies stay on prev_skill unless the topic clearly changed.\n"
        f"3. Comparisons across product families, generic questions about Bangkok Bank, or unclear product type -> '{general}'.\n"
        f"4. Not about banking products at all (weather, jokes, coding) -> '{OFFTOPIC}'.\n"
        "5. confidence is 0..1 for your choice; language is the language the user wrote in.\n"
        "Output only the JSON object."
    )


def build_router_definition(settings: Settings, skills: dict[str, SkillSpec]) -> PromptAgentDefinition:
    return PromptAgentDefinition(
        model=settings.router_model,
        instructions=router_instructions(skills),
        text=PromptAgentDefinitionTextOptions(format=TextResponseFormatJsonSchema(name="route", schema=route_schema(skills), strict=True)),
    )


def keyword_route(question: str, skills: dict[str, SkillSpec]) -> RouteDecision:
    q = question.lower()
    best, best_hits = None, 0
    for s in skills.values():
        hits = sum(1 for k in s.keywords if k.lower() in q)
        if hits > best_hits:
            best, best_hits = s, hits
    if best is None:
        fallback = "general" if "general" in skills else sorted(skills)[0]
        return RouteDecision(skill_id=fallback, confidence=0.2, reason="keyword fallback: no keyword matched")
    return RouteDecision(skill_id=best.id, confidence=min(0.9, 0.4 + 0.15 * best_hits), reason=f"keyword fallback: {best_hits} keyword hit(s)")


def route(openai_client, question: str, history: list[dict[str, str]], prev_skill: str | None, skills: dict[str, SkillSpec]) -> RouteDecision:
    payload = {
        "prev_skill": prev_skill,
        "recent_turns": [t for t in history[-6:] if t.get("role") == "user"][-3:],
        "message": question,
    }
    t0 = time.perf_counter()
    try:
        resp = openai_client.responses.create(
            input=json.dumps(payload, ensure_ascii=False),
            extra_body={"agent_reference": {"name": ROUTER_AGENT, "type": "agent_reference"}},
        )
        raw = resp.output_text.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0) if m else raw)
        skill_id = str(data.get("skill_id", ""))
        if skill_id != OFFTOPIC and skill_id not in skills:
            raise ValueError(f"router returned unknown skill '{skill_id}'")
        return RouteDecision(
            skill_id=skill_id,
            confidence=float(data.get("confidence", 0.5)),
            language=str(data.get("language", "th")),
            reason=str(data.get("reason", "")),
            usage=usage_dict(getattr(resp, "usage", None)),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as e:  # noqa: BLE001 - degrade to keywords, never block the chat
        d = keyword_route(question, skills)
        d.reason = f"{d.reason} (router agent failed: {type(e).__name__}: {str(e)[:120]})"
        d.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return d


def usage_dict(usage: Any) -> dict[str, int]:
    """Normalise a Responses API usage object to plain ints."""
    if usage is None:
        return {}
    g = lambda obj, name: getattr(obj, name, None) if obj is not None else None  # noqa: E731
    out = {
        "input_tokens": g(usage, "input_tokens") or 0,
        "output_tokens": g(usage, "output_tokens") or 0,
        "total_tokens": g(usage, "total_tokens") or 0,
        "cached_tokens": g(g(usage, "input_tokens_details"), "cached_tokens") or 0,
        "reasoning_tokens": g(g(usage, "output_tokens_details"), "reasoning_tokens") or 0,
    }
    return {k: int(v) for k, v in out.items()}
