"""Skill router: one chat-model call with strict JSON output, plus a keyword fallback."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .config import Settings
from .llm import usage_from_message
from .models import ROUTER_AGENT, AgentDefinition, RouteDecision, SkillSpec

OFFTOPIC = "offtopic"


def route_schema(skills: dict[str, SkillSpec]) -> dict[str, Any]:
    ids = sorted(skills) + [OFFTOPIC]
    return {
        "title": "route",  # langchain-openai needs a name on a dict schema to turn it into a response format
        "description": "Which skill answers the customer's message.",
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
        f"3. Comparisons across product families, questions that compare Bangkok Bank with another bank, generic questions "
        f"about Bangkok Bank, or unclear product type -> '{general}'.\n"
        f"4. Not about banking products at all (weather, jokes, coding) -> '{OFFTOPIC}'.\n"
        "5. confidence is 0..1 for your choice; language is the language the user wrote in.\n"
        "Output only the JSON object."
    )


def build_router_definition(settings: Settings, skills: dict[str, SkillSpec]) -> AgentDefinition:
    return AgentDefinition(
        model=settings.router_model,
        instructions=router_instructions(skills),
        response_format={"type": "json_schema", "name": "route", "schema": route_schema(skills), "strict": True},
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


def _parse(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = raw if isinstance(raw, str) else str(getattr(raw, "content", raw) or "")
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def route(llm, question: str, history: list[dict[str, str]], prev_skill: str | None, skills: dict[str, SkillSpec],
          instructions: str | None = None) -> RouteDecision:
    """Ask the router model; on any failure degrade to keywords (a broken router must never block the chat)."""
    payload = {
        "prev_skill": prev_skill,
        "recent_turns": [t for t in history[-6:] if t.get("role") == "user"][-3:],
        "message": question,
    }
    t0 = time.perf_counter()
    try:
        structured = llm.with_structured_output(route_schema(skills), method="json_schema", strict=True, include_raw=True)
        out = structured.invoke([SystemMessage(content=instructions or router_instructions(skills)),
                                 HumanMessage(content=json.dumps(payload, ensure_ascii=False))])
        raw_msg = out.get("raw") if isinstance(out, dict) else None
        data = out.get("parsed") if isinstance(out, dict) else None
        if data is None:
            if isinstance(out, dict) and out.get("parsing_error"):
                raise out["parsing_error"]
            data = _parse(raw_msg if raw_msg is not None else out)
        if not isinstance(data, dict):
            data = data.model_dump() if hasattr(data, "model_dump") else dict(data)
        skill_id = str(data.get("skill_id", ""))
        if skill_id != OFFTOPIC and skill_id not in skills:
            raise ValueError(f"router returned unknown skill '{skill_id}'")
        return RouteDecision(
            skill_id=skill_id,
            confidence=float(data.get("confidence", 0.5)),
            language=str(data.get("language", "th")),
            reason=str(data.get("reason", "")),
            usage=usage_from_message(raw_msg),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as e:  # noqa: BLE001 - degrade to keywords, never block the chat
        d = keyword_route(question, skills)
        d.reason = f"{d.reason} (router model failed: {type(e).__name__}: {str(e)[:120]})"
        d.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return d


def usage_dict(usage: Any) -> dict[str, int]:
    """Normalise a usage object (LangChain `usage_metadata` dict, an AIMessage, or an OpenAI-style object) to plain ints."""
    if usage is None:
        return {}
    if isinstance(usage, dict) or hasattr(usage, "usage_metadata"):
        return usage_from_message(usage)
    g = lambda obj, name: getattr(obj, name, None) if obj is not None else None  # noqa: E731
    out = {
        "input_tokens": g(usage, "input_tokens") or 0,
        "output_tokens": g(usage, "output_tokens") or 0,
        "total_tokens": g(usage, "total_tokens") or 0,
        "cached_tokens": g(g(usage, "input_tokens_details"), "cached_tokens") or 0,
        "reasoning_tokens": g(g(usage, "output_tokens_details"), "reasoning_tokens") or 0,
    }
    return {k: int(v) for k, v in out.items()}
