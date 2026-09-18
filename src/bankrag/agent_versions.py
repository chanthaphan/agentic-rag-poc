"""Local registry of agent definition versions (table `agent_versions` in the sessions database).

`bankrag skills sync` publishes a new version of an agent only when the hash of its desired definition changed; the
runtime answers with the latest published version, so editing a SKILL.md does not change the live agent until it is
synced - the same contract the Foundry agent versions had, now in SQLite next to the sessions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from . import sessions as SESS
from .config import Settings
from .models import AgentDefinition


def _row(r) -> dict[str, Any]:
    agent, version, skill_id, created_at, spec_hash, model, instructions, tools, definition, metadata, description = r
    return {
        "agent": agent, "version": str(version), "skill_id": skill_id, "created_at": created_at, "spec_hash": spec_hash, "model": model,
        "instructions": instructions, "tools": json.loads(tools or "[]"), "definition": json.loads(definition or "{}"),
        "metadata": json.loads(metadata or "{}"), "description": description or "",
    }


_COLS = "agent,version,skill_id,created_at,spec_hash,model,instructions,tools,definition,metadata,description"


def latest(settings: Settings, agent: str) -> Optional[dict[str, Any]]:
    with SESS._lock, SESS.connect(settings) as con:
        r = con.execute(f"SELECT {_COLS} FROM agent_versions WHERE agent=? ORDER BY version DESC LIMIT 1", (agent,)).fetchone()
    return _row(r) if r else None


def create(settings: Settings, agent: str, definition: AgentDefinition, metadata: dict[str, Any], description: str = "") -> int:
    d = definition.as_dict()
    with SESS._lock, SESS.connect(settings) as con:
        cur = con.execute("SELECT COALESCE(MAX(version), 0) FROM agent_versions WHERE agent=?", (agent,)).fetchone()
        version = int(cur[0]) + 1
        con.execute(
            f"INSERT INTO agent_versions({_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (agent, version, str(metadata.get("skill_id") or ""), datetime.now(timezone.utc).isoformat(), str(metadata.get("spec_hash") or ""),
             definition.model, definition.instructions, json.dumps(d.get("tools", []), ensure_ascii=False),
             json.dumps(d, ensure_ascii=False, sort_keys=True), json.dumps(metadata, ensure_ascii=False), description),
        )
    return version


def list_versions(settings: Settings, agent: str) -> list[dict[str, Any]]:
    """Newest first; the shape the Studio versions table reads (tools as their types)."""
    with SESS._lock, SESS.connect(settings) as con:
        rows = con.execute(f"SELECT {_COLS} FROM agent_versions WHERE agent=? ORDER BY version DESC", (agent,)).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        out.append({"version": d["version"], "created_at": d["created_at"], "model": d["model"], "metadata": d["metadata"],
                    "description": d["description"], "tools": [t.get("type") for t in d["tools"]]})
    return out


def get_version(settings: Settings, agent: str, version: str | int) -> Optional[dict[str, Any]]:
    try:
        v = int(version)
    except (TypeError, ValueError):
        return None
    with SESS._lock, SESS.connect(settings) as con:
        r = con.execute(f"SELECT {_COLS} FROM agent_versions WHERE agent=? AND version=?", (agent, v)).fetchone()
    return _row(r) if r else None


def prune(settings: Settings, agent: str, keep: int) -> int:
    """Keep only the `keep` newest versions; returns how many were deleted."""
    if keep <= 0:
        return 0
    with SESS._lock, SESS.connect(settings) as con:
        versions = [int(r[0]) for r in con.execute("SELECT version FROM agent_versions WHERE agent=? ORDER BY version DESC", (agent,)).fetchall()]
        old = versions[keep:]
        for v in old:
            con.execute("DELETE FROM agent_versions WHERE agent=? AND version=?", (agent, v))
    return len(old)


def delete_agent(settings: Settings, agent: str) -> int:
    with SESS._lock, SESS.connect(settings) as con:
        n = con.execute("SELECT COUNT(*) FROM agent_versions WHERE agent=?", (agent,)).fetchone()[0]
        con.execute("DELETE FROM agent_versions WHERE agent=?", (agent,))
    return int(n)


def list_agents(settings: Settings) -> list[dict[str, Any]]:
    """Every agent with its latest version (for prune: which published agents no longer have a skill folder)."""
    with SESS._lock, SESS.connect(settings) as con:
        rows = con.execute(
            f"SELECT {_COLS} FROM agent_versions a WHERE version = (SELECT MAX(version) FROM agent_versions b WHERE b.agent = a.agent) ORDER BY agent"
        ).fetchall()
    return [_row(r) for r in rows]


def to_definition(row: dict[str, Any]) -> AgentDefinition:
    return AgentDefinition.model_validate(row["definition"]) if row.get("definition") else AgentDefinition(model=row["model"], instructions=row["instructions"], tools=row.get("tools") or [])
