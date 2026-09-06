from pathlib import Path

from bankrag.config import Settings
from bankrag.foundry_sync import desired_definition, spec_hash
from bankrag.router import build_router_definition, keyword_route, route_schema
from bankrag.skills import load_base, load_skills

ROOT = Path(__file__).resolve().parents[1]


def _settings(**over) -> Settings:
    s = Settings.load(ROOT)
    s.project_endpoint = "https://x.services.ai.azure.com/api/projects/p"
    s.search_endpoint = "https://s.search.windows.net"
    s.search_query_key = "qk"
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_spec_hash_is_stable_and_sensitive():
    s = _settings()
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    spec = skills["credit-card"]
    h1 = spec_hash(desired_definition(s, spec, base))
    h2 = spec_hash(desired_definition(s, spec, base))
    assert h1 == h2 and len(h1) == 16
    spec.body += "\nextra rule"
    assert spec_hash(desired_definition(s, spec, base)) != h1


def test_definition_uses_identity_connection_or_apikey_header():
    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    spec = skills["insurance"]
    d = desired_definition(_settings(), spec, base).as_dict()
    tool = d["tools"][0]
    assert tool["type"] == "mcp" and tool["project_connection_id"] == "kb-insurance-mcp"
    assert tool["allowed_tools"] == ["knowledge_base_retrieve"] and "kb-insurance/mcp" in tool["server_url"]
    d2 = desired_definition(_settings(kb_mcp_auth="apikey"), spec, base).as_dict()
    assert d2["tools"][0]["headers"] == {"api-key": "qk"} and "project_connection_id" not in d2["tools"][0]


def test_router_definition_and_keyword_fallback():
    skills = load_skills(ROOT / "skills")
    schema = route_schema(skills)
    assert "offtopic" in schema["properties"]["skill_id"]["enum"] and "credit-card" in schema["properties"]["skill_id"]["enum"]
    d = build_router_definition(_settings(), skills).as_dict()
    assert d["text"]["format"]["type"] == "json_schema" and d["text"]["format"]["strict"] is True
    assert keyword_route("บัตรเครดิต Infinite เข้าเลานจ์", skills).skill_id == "credit-card"
    assert keyword_route("กองทุนรวม RMF", skills).skill_id == "wealth"
    assert keyword_route("hello there", skills).skill_id == "general"


def test_skill_without_documents_has_no_tool():
    from bankrag.foundry_sync import plan_kb_owners

    skills, base = load_skills(ROOT / "skills"), load_base(ROOT / "skills")
    s = _settings()
    from bankrag.models import SkillSpec

    skills["ghost"] = SkillSpec(id="ghost", name="Ghost", description="no docs category", product_category="ghost-category", keywords=["x"])
    owners = plan_kb_owners(s, skills)
    assert owners["credit-card"].id == "credit-card" and owners["general"].id == "general"
    assert owners["ghost"].id == "general"
    d = desired_definition(s, skills["ghost"], base, owners["ghost"]).as_dict()
    assert d.get("tools", []) == [] and "No documents have been uploaded" in d["instructions"]
    d2 = desired_definition(s, skills["credit-card"], base, owners["credit-card"]).as_dict()
    assert d2["tools"][0]["type"] == "mcp"
    d3 = desired_definition(s, skills["ghost"], base, owners["ghost"], shared_by_quota=True).as_dict()
    assert d3["tools"][0]["type"] == "mcp" and "kb-general" in d3["tools"][0]["server_url"] and "quota" in d3["instructions"]
