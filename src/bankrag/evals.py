"""Evaluation runs (routing accuracy, grounded answers, model comparison) shared by the CLI and Studio."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .config import Settings
from .ingest.progress import report as progress
from .models import SkillSpec

Log = Callable[[str], None]
SETS = {"routing": "routing_questions.yaml", "rag": "rag_questions.yaml"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def eval_path(settings: Settings, set_name: str) -> Path:
    if set_name not in SETS:
        raise ValueError("unknown eval set")
    return settings.evals_dir / SETS[set_name]


def load_cases(settings: Settings, set_name: str) -> list[dict]:
    p = eval_path(settings, set_name)
    return list(yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []


def save_cases(settings: Settings, set_name: str, cases: list[dict]) -> list[dict]:
    clean = []
    for c in cases:
        q = str(c.get("q", "")).strip()
        if not q:
            continue
        if set_name == "routing":
            clean.append({"q": q, "skill": str(c.get("skill", "")).strip()})
        else:
            row = {"q": q, "expect": [str(x) for x in (c.get("expect") or []) if str(x).strip()]}
            if c.get("skill"):
                row["skill"] = str(c["skill"])
            if c.get("require_source") is False:
                row["require_source"] = False
            clean.append(row)
    p = eval_path(settings, set_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(clean, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return clean


def new_run(set_name: str) -> dict:
    return {"id": uuid.uuid4().hex[:10], "set": set_name, "started_at": _now(), "rows": [], "summary": {}}


def run_routing(settings: Settings, skills: dict[str, SkillSpec], cases: list[dict], *, openai_client=None, log: Log = print) -> dict:
    from . import router as R
    from .pricing import load_pricing, usage_cost

    if openai_client is None:
        from .foundry import project_client

        openai_client = project_client(settings).get_openai_client()
    pricing = load_pricing(settings)
    run = new_run("routing")
    ok = 0
    progress(log, "questions", 0, len(cases), message="", passed=0, failed=0)
    for i, c in enumerate(cases):
        progress(log, "questions", i, len(cases), message=c["q"][:80], passed=ok, failed=i - ok)
        t0 = time.perf_counter()
        d = R.route(openai_client, c["q"], [], None, skills)
        ms = int((time.perf_counter() - t0) * 1000)
        cost = usage_cost(pricing, settings.router_model, d.usage)["total_usd"] if d.usage else 0.0
        hit = d.skill_id == c["skill"]
        ok += hit
        run["rows"].append({"q": c["q"], "expected": c["skill"], "got": d.skill_id, "pass": hit, "confidence": d.confidence, "ms": ms, "cost_usd": cost, "detail": d.reason[:160]})
        log(f"{'ok ' if hit else 'BAD'} expected={c['skill']} got={d.skill_id} conf={d.confidence:.2f} {c['q'][:60]}")
    n = len(cases)
    progress(log, "questions", n, n, message="", passed=ok, failed=n - ok)
    run["summary"] = {"questions": n, "passed": ok, "accuracy": (ok / n) if n else 0.0, "total_cost_usd": sum(r["cost_usd"] for r in run["rows"]), "avg_ms": int(sum(r["ms"] for r in run["rows"]) / n) if n else 0}
    run["finished_at"] = _now()
    return run


def run_rag(settings: Settings, skills: dict[str, SkillSpec], cases: list[dict], *, session_factory=None, log: Log = print) -> dict:
    from .chat import ChatSession

    factory = session_factory or (lambda: ChatSession(settings, skills))
    run = new_run("rag")
    passed = 0
    progress(log, "questions", 0, len(cases), message="", passed=0, failed=0)
    for i, c in enumerate(cases):
        progress(log, "questions", i, len(cases), message=c["q"][:80], passed=passed, failed=i - passed)
        session = factory()
        t0 = time.perf_counter()
        try:
            ans = session.ask(c["q"], force_skill=c.get("skill"))
        except Exception as e:  # noqa: BLE001
            run["rows"].append({"q": c["q"], "expected": ", ".join(c.get("expect", [])), "got": f"ERROR {type(e).__name__}", "pass": False, "ms": int((time.perf_counter() - t0) * 1000), "cost_usd": 0.0, "detail": str(e)[:160]})
            continue
        ms = int((time.perf_counter() - t0) * 1000)
        expect = c.get("expect", [])
        matched = [e for e in expect if e in ans.text]
        has_src = bool(ans.citations or ans.references)
        good = len(matched) == len(expect) and (has_src or not c.get("require_source", True))
        passed += good
        run["rows"].append({"q": c["q"], "expected": ", ".join(expect) or "(any)", "got": ans.skill_id, "pass": good, "confidence": ans.confidence, "ms": ms,
                            "cost_usd": (ans.trace.get("cost") or {}).get("total_usd", 0.0), "detail": f"matched {len(matched)}/{len(expect)}; sources={'yes' if has_src else 'no'} · {ans.text[:140]}"})
        log(f"{'ok ' if good else 'BAD'} skill={ans.skill_id} matched={len(matched)}/{len(expect)} sources={has_src} {c['q'][:60]}")
        try:
            session.reset()
        except Exception:  # noqa: BLE001
            pass
    n = len(cases)
    progress(log, "questions", n, n, message="", passed=passed, failed=n - passed)
    run["summary"] = {"questions": n, "passed": passed, "pass_rate": (passed / n) if n else 0.0, "total_cost_usd": sum(r["cost_usd"] for r in run["rows"]), "avg_ms": int(sum(r["ms"] for r in run["rows"]) / n) if n else 0}
    run["finished_at"] = _now()
    return run


def run_compare(settings: Settings, skills: dict[str, SkillSpec], base_body: str, skill_id: str, models: list[str], questions: list[str], *, log: Log = print) -> dict:
    """Temporary agents bank-<skill>-cmp-<model> (same definition, model swapped); deleted afterwards."""
    import re

    from .foundry import project_client
    from .foundry_sync import SOURCE_TAG, desired_definition, ensure_agent, plan_kb_owners
    from .models import Answer
    from .chat import ChatSession, parse_response, strip_markers
    from .pricing import load_pricing, usage_cost
    from .router import usage_dict

    spec = skills[skill_id]
    client = project_client(settings)
    openai_client = client.get_openai_client()
    owner = plan_kb_owners(settings, skills)[skill_id]
    pricing = load_pricing(settings)
    run = new_run("compare")
    run["summary"] = {"skill": skill_id, "models": models, "questions": len(questions)}
    temp_agents: list[str] = []
    try:
        progress(log, "agents", 0, len(models), message="")
        for m in models:
            progress(log, "agents", models.index(m), len(models), message=m)
            name = f"{spec.agent_name}-cmp-{re.sub(r'[^a-z0-9]+', '-', m.lower()).strip('-')}"[:60]
            spec_m = spec.model_copy(update={"model": m})
            definition = desired_definition(settings, spec_m, base_body, owner)
            action, version = ensure_agent(client, name, definition, {"source": f"{SOURCE_TAG}-compare", "skill_id": skill_id}, f"temporary comparison agent for {skill_id} on {m}")
            temp_agents.append(name)
            log(f"[{m}] agent {name} {action} v{version}")
        rows = [{"q": q, "by_model": {}} for q in questions]
        total = len(models) * len(questions)
        done = 0
        progress(log, "questions", 0, total, message="")
        for m, name in zip(models, temp_agents):
            for row in rows:
                progress(log, "questions", done, total, message=f"{m}: {row['q'][:60]}")
                done += 1
                t0 = time.perf_counter()
                try:
                    conv = openai_client.conversations.create()
                    resp = openai_client.responses.create(conversation=conv.id, input=row["q"], extra_body={"agent_reference": {"name": name, "type": "agent_reference"}})
                    text, _c, _t, extra = parse_response(resp)
                    usage = usage_dict(getattr(resp, "usage", None))
                    cost = usage_cost(pricing, m, usage)["total_usd"]
                    row["by_model"][m] = {"text": strip_markers(text), "ms": int((time.perf_counter() - t0) * 1000), "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                                          "cost_usd": cost, "retrieval_calls": extra["retrieval"]["calls"], "documents": extra["retrieval"]["documents"]}
                    try:
                        openai_client.conversations.delete(conversation_id=conv.id)
                    except Exception:  # noqa: BLE001
                        pass
                    log(f"[{m}] {row['q'][:50]} -> {row['by_model'][m]['ms']} ms, ${cost:.4f}")
                except Exception as e:  # noqa: BLE001
                    row["by_model"][m] = {"error": f"{type(e).__name__}: {str(e)[:200]}", "ms": int((time.perf_counter() - t0) * 1000), "cost_usd": 0.0}
                    log(f"[{m}] ERROR {row['by_model'][m]['error']}")
        run["rows"] = rows
        progress(log, "cleanup", total, total, message="deleting temporary agents")
        for m in models:
            vals = [r["by_model"].get(m, {}) for r in rows]
            run["summary"][f"{m} avg_ms"] = int(sum(v.get("ms", 0) for v in vals) / max(1, len(vals)))
            run["summary"][f"{m} cost_usd"] = sum(v.get("cost_usd", 0.0) for v in vals)
    finally:
        for name in temp_agents:
            try:
                client.agents.delete(name, force=True)
                log(f"deleted temporary agent {name}")
            except Exception as e:  # noqa: BLE001
                log(f"could not delete {name}: {e}")
    run["finished_at"] = _now()
    return run
