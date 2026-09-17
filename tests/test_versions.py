"""The local agent version registry behind `skills sync` and the Studio versions panel."""
from pathlib import Path

import pytest

from bankrag import agent_versions as AV
from bankrag.config import Settings
from bankrag.models import AgentDefinition
from bankrag.skills import load_base, load_skills
from bankrag.sync import desired_definition, ensure_agent, plan_kb_owners, runtime_definition, spec_hash, status, sync_skills

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path):
    s = Settings.load(ROOT)
    s.state_dir = tmp_path / ".state"
    s.search_endpoint = "https://s.search.windows.net"
    return s


def test_ensure_agent_created_unchanged_updated_and_pruned(settings):
    d1 = AgentDefinition(model="gpt-4.1-mini", instructions="one", tools=[{"type": "mcp", "kb_name": "kb-x"}])
    assert ensure_agent(settings, "bank-x", d1, {"source": "bankrag", "skill_id": "x"}, "x") == ("created", "1")
    assert ensure_agent(settings, "bank-x", d1, {"source": "bankrag", "skill_id": "x"}, "x") == ("unchanged", "1")
    d2 = d1.model_copy(update={"instructions": "two"})
    assert ensure_agent(settings, "bank-x", d2, {"source": "bankrag", "skill_id": "x"}, "x") == ("updated", "2")
    d3 = d1.model_copy(update={"instructions": "three"})
    assert ensure_agent(settings, "bank-x", d3, {"source": "bankrag", "skill_id": "x"}, "x", keep=2) == ("updated", "3")
    rows = AV.list_versions(settings, "bank-x")
    assert [r["version"] for r in rows] == ["3", "2"] and rows[0]["tools"] == ["mcp"] and rows[0]["metadata"]["spec_hash"] == spec_hash(d3)
    v = AV.get_version(settings, "bank-x", "2")
    assert v["instructions"] == "two" and v["model"] == "gpt-4.1-mini" and AV.to_definition(v) == d2
    assert AV.get_version(settings, "bank-x", "9") is None and AV.get_version(settings, "bank-x", "nope") is None
    assert AV.delete_agent(settings, "bank-x") == 2 and AV.latest(settings, "bank-x") is None


def test_sync_publishes_every_agent_and_status_tracks_edits(settings):
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    report = sync_skills(settings, skills, base, skip_kb=True, log=lambda m: None)
    actions = {r.agent: r.action for r in report.rows}
    assert all(a == "created" for a in actions.values()) and "bank-router" in actions and "bank-concierge" in actions
    assert {r.version for r in report.rows} == {"1"}
    assert all(r["state"] == "in-sync" for r in status(settings, skills, base))
    again = sync_skills(settings, skills, base, skip_kb=True, log=lambda m: None)
    assert {r.action for r in again.rows} == {"unchanged"}
    skills["credit-card"].body += "\nbe brief"
    st = {r["id"]: r["state"] for r in status(settings, skills, base)}
    assert st["credit-card"] == "outdated" and st["insurance"] == "in-sync"
    third = sync_skills(settings, skills, base, only="credit-card", skip_kb=True, log=lambda m: None)
    assert {r.agent: (r.action, r.version) for r in third.rows}["bank-credit-card"] == ("updated", "2")
    assert (settings.state_dir / "sync_state.json").exists()


def test_runtime_uses_the_published_version_or_the_live_composition(settings):
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    owners = plan_kb_owners(settings, skills)
    spec = skills["credit-card"]
    live, version = runtime_definition(settings, spec, base, owners)
    assert version == "" and live == desired_definition(settings, spec, base, owners[spec.id])
    sync_skills(settings, skills, base, skip_kb=True, log=lambda m: None)
    spec.body += "\nunpublished edit"
    pub, version = runtime_definition(settings, spec, base, owners)
    assert version == "1" and "unpublished edit" not in pub.instructions


def test_prune_drops_agents_whose_skill_folder_is_gone(settings):
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    from bankrag.models import SkillSpec

    ghost = SkillSpec(id="ghost", name="Ghost", description="d", product_category="all", keywords=["x"])
    sync_skills(settings, {**skills, "ghost": ghost}, base, skip_kb=True, log=lambda m: None)
    assert AV.latest(settings, "bank-ghost") is not None
    report = sync_skills(settings, skills, base, skip_kb=True, prune=True, log=lambda m: None)
    assert any(r.action == "pruned" and r.agent == "bank-ghost" for r in report.rows) and AV.latest(settings, "bank-ghost") is None


def test_versions_routes(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from bankrag import api

    monkeypatch.setattr(api.settings, "state_dir", settings.state_dir)
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    sync_skills(api.settings, skills, base, skip_kb=True, log=lambda m: None)
    c = TestClient(api.app)
    rows = c.get("/skills/credit-card/versions").json()
    assert rows and rows[0]["version"] == "1" and rows[0]["tools"] and rows[0]["created_at"]
    d = c.get("/skills/credit-card/versions/1").json()
    assert d["instructions"] == d["local_instructions"] and d["model"] == d["local_model"] and d["tools"][0]["type"] == "mcp"
    assert c.get("/skills/credit-card/versions/7").status_code == 404
    assert c.get("/skills/nope/versions").status_code == 404
    assert {r["state"] for r in c.get("/skills").json()} == {"in-sync"}
