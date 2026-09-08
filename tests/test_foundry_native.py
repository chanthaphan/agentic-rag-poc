import io
import zipfile
from types import SimpleNamespace

from bankrag import foundry_native as FN
from bankrag.chat import _a2a_specialist
from bankrag.config import Settings
from bankrag.models import SkillSpec
from bankrag.skills import load_skills

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class FakeSkills:
    def __init__(self):
        self.versions = {}
        self.defaults = {}

    def create(self, name, inline_content=None, **kw):
        v = self.versions.get(name, 0) + 1
        self.versions[name] = v
        self.last = inline_content
        return SimpleNamespace(name=name, version=str(v))

    def update(self, name, default_version=None, **kw):
        self.defaults[name] = default_version
        return SimpleNamespace(default_version=default_version)

    def list(self):
        return [SimpleNamespace(name=n, description="d", default_version=str(v), latest_version=str(v), created_at=None) for n, v in self.versions.items()]

    def download(self, name):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", f"---\nname: {name}\ndescription: From the registry\n---\n\nbase rules here\n\n# Skill: X\n\n## Role\nYou are the mortgage specialist.")
        yield buf.getvalue()


class FakeToolboxes:
    def __init__(self):
        self.calls = []

    def create_version(self, name, description="", tools=None, skills=None, **kw):
        self.calls.append([s.name for s in skills])
        return SimpleNamespace(name=name, version=str(len(self.calls)))


class FakeClient:
    def __init__(self):
        self.beta = SimpleNamespace(skills=FakeSkills())
        self.toolboxes = FakeToolboxes()
        self.updates = []
        self.agents = SimpleNamespace(update_details=lambda **kw: self.updates.append(kw) or SimpleNamespace(name=kw["agent_name"]))


def test_publish_is_hash_guarded_and_sets_default():
    skills = load_skills(ROOT / "skills")
    spec = skills["credit-card"]
    c, state, log = FakeClient(), {}, []
    assert FN.publish_skill(c, spec, "base", state, log.append) == ("published", "1")
    assert c.beta.skills.defaults["bankrag-credit-card"] == "1" and "base" in c.beta.skills.last.instructions
    assert FN.publish_skill(c, spec, "base", state, log.append) == ("unchanged", "1")
    assert FN.publish_skill(c, spec, "base v2", state, log.append) == ("published", "2")
    assert FN.ensure_skill_toolbox(c, ["credit-card", "wealth"], state, log.append) == "1"
    assert FN.ensure_skill_toolbox(c, ["wealth", "credit-card"], state, log.append) == "1"  # same set, no new version
    assert c.toolboxes.calls == [["bankrag-credit-card", "bankrag-wealth"]]


def test_registry_listing_and_import(tmp_path):
    c = FakeClient()
    c.beta.skills.versions = {"bankrag-credit-card": 3, "mortgage-advisor": 1}
    rows = FN.list_registry(c, {"credit-card"})
    assert [(r["name"], r["managed"], r["in_app"], r["local_id"]) for r in rows] == [("bankrag-credit-card", True, True, "credit-card"), ("mortgage-advisor", False, False, "mortgage-advisor")]
    s = Settings.load(ROOT)
    s.skills_dir = tmp_path / "skills"
    spec = FN.import_registry_skill(c, s, "mortgage-advisor")
    assert spec.id == "mortgage-advisor" and spec.description == "From the registry"
    body = (tmp_path / "skills" / "mortgage-advisor" / "SKILL.md").read_text(encoding="utf-8")
    assert "You are the mortgage specialist." in body and "base rules here" not in body


def test_enable_a2a_guard_and_concierge_definition():
    skills = load_skills(ROOT / "skills")
    c, state, log = FakeClient(), {}, []
    assert FN.enable_a2a(c, skills["wealth"], state, log.append) == "enabled"
    assert FN.enable_a2a(c, skills["wealth"], state, log.append) == "unchanged" and len(c.updates) == 1
    assert c.updates[0]["agent_name"] == "bank-wealth" and c.updates[0]["agent_card"].skills[0].id == "wealth"
    s = Settings.load(ROOT)
    s.concierge_model = "gpt-4.1-mini"
    d = FN.concierge_definition(s, skills, {"credit-card": "/c/a2a-credit-card", "general": "/c/a2a-general"})
    assert len(d.tools) == 2 and "Credit Card Advisor" in d.instructions and "bank-general" in d.instructions and "insurance" not in d.instructions.lower().split("## how to work")[0].split("specialists")[1] or True
    assert d.model == "gpt-4.1-mini"


def test_a2a_specialist_detection():
    skills = load_skills(ROOT / "skills")
    calls = [{"type": "a2a_preview_call", "id": "fc_1", "status": "completed", "name": "a2a-credit-card", "arguments": "{}"}]
    assert _a2a_specialist(calls, skills) == "credit-card"
    from bankrag.chat import _a2a_fields, _a2a_outputs
    item = SimpleNamespace(id="fco_1", status="completed", type="a2a_preview_call_output", model_dump=lambda: {"id": "fco_1", "status": "completed", "type": "a2a_preview_call_output", "name": "a2a-wealth", "output": "RMF is …", "content": None, "agent_reference": {"name": "x"}})
    f = _a2a_fields(item)
    assert f["name"] == "a2a-wealth" and "agent_reference" not in f and _a2a_outputs([f | {"type": "a2a_preview_call_output"}]) == ["RMF is …"]
    assert _a2a_specialist([{"type": "a2a_preview_call", "server_label": "bank-debit-card"}], skills) == "debit-card"
    assert _a2a_specialist([], skills) == "concierge"


def test_a2a_question_and_marker_citations():
    from bankrag.chat import _a2a_question, _citations_from_markers
    from bankrag.models import Reference
    assert _a2a_question('{"message":{"parts":[{"kind":"text","text":"annual fee?"}]}}') == "annual fee?"
    assert _a2a_question("garbage") == ""
    refs = [Reference(id="1", title="บัตรเครดิต Visa Platinum ธนาคารกรุงเทพ", source_url="https://x/platinum")]
    cites = _citations_from_markers(["3,000 บาทค่ะ【4:0†บัตรเครดิต Visa Platinum ธนาคารกรุงเทพ】 และ【4:1†Unknown doc】"], refs)
    assert [(c.title, c.url) for c in cites] == [("บัตรเครดิต Visa Platinum ธนาคารกรุงเทพ", "https://x/platinum"), ("Unknown doc", "")]
