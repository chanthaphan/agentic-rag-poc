import shutil
from pathlib import Path

import pytest

from bankrag.config import Settings
from bankrag import rules as RL
from bankrag import rules_xlsx as RX
from bankrag.models import RuleSpec
from bankrag.skills import compose_instructions, load_base, load_skills

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """A copy of the repo's rules so tests can write without touching the real pack."""
    s = Settings.load(ROOT)
    shutil.copytree(ROOT / "rules", tmp_path / "rules")
    s.rules_dir = tmp_path / "rules"
    RL._cache.clear()
    return s


def test_repo_pack_loads_and_validates(settings):
    pack = RL.load_pack(settings)
    assert len(pack.rules) == 12 and len(pack.products) == 5
    for rid, (errors, _) in RL.validate_pack(pack).items():
        assert errors == [], (rid, errors)
    rule = next(r for r in pack.rules if r.id == "credit-card-use-warning")
    assert rule.legal_text.startswith("ข้อความหรือรูปภาพโฆษณา") and rule.system_rule and rule.assistant_note
    assert rule.check == "required_phrase" and rule.enforcement == "append"


def test_product_detection_uses_text_then_skill(settings):
    pack = RL.load_pack(settings)
    assert RL.detect_products(pack, "ขอทราบค่าธรรมเนียมบัตรเครดิตหน่อย") == ["credit-card-bbl"]
    assert "home-loan" in RL.detect_products(pack, "สนใจสินเชื่อบ้าน")
    assert RL.detect_products(pack, "What is a mortgage?") == ["home-loan"]
    assert RL.detect_products(pack, "สวัสดีค่ะ") == []  # nothing regulated in the text
    assert RL.detect_products(pack, "สวัสดีค่ะ", skill_id="credit-card") == ["credit-card-bbl"]  # the specialist's own family


def test_missing_warning_is_appended_verbatim(settings):
    text = "บัตรเครดิต Visa Platinum ค่าธรรมเนียมรายปี 3,000 บาทค่ะ"
    fixed, report = RL.guard(settings, text, question="ค่าธรรมเนียมเท่าไหร่", language="th", skill_id="credit-card")
    warning = "ใช้เท่าที่จำเป็นและชำระคืนได้เต็มจำนวนตามกำหนด จะได้ไม่เสียดอกเบี้ย"
    assert warning in fixed and fixed.startswith(text)
    assert report["fixed"] == ["credit-card-use-warning"] and report["violations"] == []
    assert report["appended"].strip().endswith(warning)
    again, report2 = RL.guard(settings, fixed, language="th", skill_id="credit-card")
    assert again == fixed and report2["fixed"] == []  # already compliant: nothing added twice


def test_prohibited_wording_and_missing_figures_are_reported(settings):
    text = "สินเชื่อบ้าน ดอกเบี้ยเริ่มต้น 2.75% ต่อปี ผ่อนเพียง 4,500 บาทต่อเดือน อนุมัติง่ายค่ะ"
    _, report = RL.guard(settings, text, language="th", skill_id="general")
    assert set(report["violations"]) >= {"no-over-indebtedness-wording", "effective-rate-range", "instalment-assumptions", "reference-rate-and-date"}
    finding = next(f for f in report["findings"] if f["rule_id"] == "no-over-indebtedness-wording")
    assert "อนุมัติง่าย" in finding["detail"] and finding["severity"] == "block"


def test_conditional_rules_are_not_applicable_without_the_figure(settings):
    _, report = RL.guard(settings, "สินเชื่อบ้านบัวหลวงมีให้เลือกหลายแบบค่ะ ติดต่อสาขาเพื่อดูเงื่อนไขได้เลย", language="th", skill_id="general")
    verdicts = {f["rule_id"]: f["verdict"] for f in report["findings"]}
    assert verdicts["instalment-assumptions"] == "not_applicable"
    assert verdicts["effective-rate-range"] == "not_applicable"
    assert report["violations"] == []


def test_unregulated_answer_is_left_alone(settings):
    text = "บัตรเดบิตบี เฟิสต์ สมาร์ท ค่าธรรมเนียมรายปี 300 บาทค่ะ"
    fixed, report = RL.guard(settings, text, language="th", skill_id="debit-card")
    assert fixed == text and report["checked"] == 0


def test_prompt_blocks_carry_the_rules(settings):
    pack = RL.load_pack(settings)
    skills = load_skills(ROOT / "skills")
    card = RL.prompt_block_for_skill(pack, skills["credit-card"])
    assert "ใช้เท่าที่จำเป็นและชำระคืนได้เต็มจำนวนตามกำหนด" in card and "เอกสารแนบ 2 ข้อ 2.3.3 (1)" in card
    assert "สินเชื่อบ้าน" not in card  # only the families this skill answers about
    assert RL.prompt_block_for_skill(pack, skills["debit-card"]) == ""
    concierge = RL.prompt_block_for_concierge(pack)
    assert "บัตรเครดิตของธนาคารกรุงเทพ" in concierge and "สินเชื่อบ้าน" in concierge
    text = compose_instructions(load_base(ROOT / "skills"), skills["credit-card"], card)
    assert text.index("# Skill: Credit Card Advisor") < text.index(RL.HEADER)


def test_xlsx_round_trip_keeps_the_check_configuration(settings):
    before = RL.load_pack(settings)
    data = RX.export_xlsx(settings)
    RL._cache.clear()
    result = RX.import_xlsx(settings, data)
    assert result["created"] == [] and result["updated"] == []
    after = RL.load_pack(settings)
    assert [r.model_dump(exclude={"path"}) for r in after.rules] == [r.model_dump(exclude={"path"}) for r in before.rules]


def test_import_keeps_engineering_fields_and_flags_unknown_products(settings, tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["เล่มกฎหมาย", "ข้อกฎหมาย", "กฎหมาย", "กฎสำหรับระบบ", "ผลิตภัณฑ์ที่ต้องตรวจสอบ", "สถานะ"])
    ws.append(["ประกาศ ธปท. 3/2568", "เอกสารแนบ 2 ข้อ 2.3.3 (1)", "ข้อความหรือรูปภาพโฆษณาที่กล่าวถึงบัตรเครดิต…", "ปรับถ้อยคำใหม่จากทีม compliance", "บัตรเครดิตของธนาคารกรุงเทพ", "active"])
    ws.append(["ประกาศ ธปท. 3/2568", "เอกสารแนบ 2 ข้อ 9.9", "กฎใหม่", "ห้ามทำแบบนี้", "สินเชื่อรถยนต์", "draft"])
    path = tmp_path / "sheet.xlsx"
    wb.save(path)

    result = RX.import_xlsx(settings, path.read_bytes())
    assert result["updated"] == ["credit-card-use-warning"] and result["created"] == ["rule-2-9-9"]
    assert result["new_products"] and any("สินเชื่อรถยนต์" in w for w in result["warnings"])
    RL._cache.clear()
    pack = RL.load_pack(settings)
    kept = next(r for r in pack.rules if r.id == "credit-card-use-warning")
    assert kept.system_rule == "ปรับถ้อยคำใหม่จากทีม compliance"  # the sheet owns the legal columns
    assert kept.check == "required_phrase" and kept.enforcement == "append" and kept.phrases and kept.assistant_note
    fresh = next(r for r in pack.rules if r.id == "rule-2-9-9")
    assert fresh.check == "judgement" and fresh.status == "draft" and fresh.products == result["new_products"]


def test_retired_rules_stop_applying(settings):
    pack = RL.load_pack(settings)
    rule = next(r for r in pack.rules if r.id == "credit-card-use-warning")
    RL.write_rule(settings, "mccs", RuleSpec(**{**rule.model_dump(), "status": "retired"}))
    fixed, report = RL.guard(settings, "บัตรเครดิตใบนี้ค่าธรรมเนียมรายปี 3,000 บาท", language="th", skill_id="credit-card")
    assert fixed.count("ใช้เท่าที่จำเป็น") == 0 and "credit-card-use-warning" not in report["fixed"]


def test_update_rule_rejects_a_broken_change(settings):
    with pytest.raises(ValueError):
        RL.update_rule(settings, "mccs", "credit-card-use-warning", {"patterns": ["("], "check": "required_pattern"})
    RL.update_rule(settings, "mccs", "credit-card-use-warning", {"status": "draft"})
    assert RL.load_pack(settings).rules and next(r for r in RL.load_pack(settings).rules if r.id == "credit-card-use-warning").status == "draft"


# ---- API ----
def _api_client(tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from bankrag import api

    shutil.copytree(ROOT / "rules", tmp_path / "rules")
    shutil.copytree(ROOT / "skills", tmp_path / "skills")
    monkeypatch.setattr(api.settings, "rules_dir", tmp_path / "rules")
    monkeypatch.setattr(api.settings, "skills_dir", tmp_path / "skills")
    monkeypatch.setattr(api.settings, "state_dir", tmp_path / ".state")
    monkeypatch.setattr(api.settings, "studio_password", "secret")
    monkeypatch.setattr(api.settings, "studio_admins", [])
    RL._cache.clear()
    return TestClient(api.app)


def test_rules_endpoints(tmp_path, monkeypatch):
    c = _api_client(tmp_path, monkeypatch)
    d = c.get("/rules").json()
    assert len(d["rules"]) == 12 and d["pack"]["id"] == "mccs"
    card = next(r for r in d["rules"] if r["id"] == "credit-card-use-warning")
    assert card["skills"] == ["credit-card"] and card["errors"] == []
    assert "credit-card" in {s for p in d["products"] for s in p["skills"]}

    block = c.get("/rules/prompt", params={"skill": "credit-card"}).json()["block"]
    assert "เอกสารแนบ 2 ข้อ 2.3.3 (1)" in block
    assert c.get("/rules/prompt").json()["target"] == "concierge"
    assert c.get("/rules/prompt", params={"skill": "nope"}).status_code == 404

    r = c.post("/rules/check", json={"text": "บัตรเครดิตใบนี้ค่าธรรมเนียมรายปี 3,000 บาท", "skill": "credit-card"}).json()
    assert r["report"]["fixed"] == ["credit-card-use-warning"] and "ใช้เท่าที่จำเป็น" in r["text"]
    assert c.post("/rules/check", json={"text": " "}).status_code == 400

    assert c.get("/rules.xlsx").headers["content-type"].startswith("application/vnd.openxml")
    assert c.put("/rules/credit-card-use-warning", json={"status": "draft"}).status_code == 401
    assert c.put("/rules/credit-card-use-warning", json={"status": "draft"}, auth=("admin", "secret")).json()["status"] == "draft"
    assert c.put("/rules/nope", json={"status": "draft"}, auth=("admin", "secret")).status_code == 404


# ---- the guard inside the chat flow ----
class _Ev:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_openai(answer_text: str):
    """Just enough of the Foundry Responses API for ChatSession.ask_stream."""
    final = _Ev(type="response", output_text=answer_text, output=[], usage=None, model="gpt-4.1-mini", id="resp_1")
    stream = [_Ev(type="response.output_text.delta", delta=answer_text), _Ev(type="response.completed", response=final)]
    return _Ev(conversations=_Ev(create=lambda **kw: _Ev(id="conv_1")),
               responses=_Ev(create=lambda **kw: iter(stream)))


def test_chat_appends_the_missing_warning_and_traces_it(settings, monkeypatch):
    from bankrag import chat as chat_mod
    from bankrag.models import RouteDecision

    text = "บัตรเครดิต Visa Platinum ค่าธรรมเนียมรายปี 3,000 บาทค่ะ"
    skills = load_skills(ROOT / "skills")
    monkeypatch.setattr(chat_mod, "plan_kb_owners", lambda s, sk: dict(sk))
    session = chat_mod.ChatSession(settings, skills, project=_Ev(get_openai_client=lambda: _fake_openai(text)))
    monkeypatch.setattr(session, "decide", lambda q, force=None: RouteDecision(skill_id="credit-card", confidence=1.0, language="th", reason="test"))

    events = list(session.ask_stream("ค่าธรรมเนียมบัตรเครดิตเท่าไหร่", with_sources=False))
    answer = next(e["answer"] for e in events if e["type"] == "done")
    warning = "ใช้เท่าที่จำเป็นและชำระคืนได้เต็มจำนวนตามกำหนด จะได้ไม่เสียดอกเบี้ย"
    assert warning in answer.text and answer.text.startswith(text)
    assert "".join(e["text"] for e in events if e["type"] == "delta") == answer.text  # the streamed text matches
    assert answer.trace["compliance"]["fixed"] == ["credit-card-use-warning"]
    assert "appended" not in answer.trace["compliance"]  # the trace keeps the report, not the payload


def test_a_broken_rule_pack_does_not_break_the_chat(settings, monkeypatch):
    from bankrag import chat as chat_mod
    from bankrag.models import RouteDecision

    monkeypatch.setattr(chat_mod.RL, "guard", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad pack")))
    monkeypatch.setattr(chat_mod, "plan_kb_owners", lambda s, sk: dict(sk))
    skills = load_skills(ROOT / "skills")
    session = chat_mod.ChatSession(settings, skills, project=_Ev(get_openai_client=lambda: _fake_openai("บัตรเครดิต")))
    monkeypatch.setattr(session, "decide", lambda q, force=None: RouteDecision(skill_id="credit-card", confidence=1.0, language="th", reason="test"))
    answer = next(e["answer"] for e in session.ask_stream("q", with_sources=False) if e["type"] == "done")
    assert answer.text == "บัตรเครดิต" and "bad pack" in answer.trace["compliance"]["error"]


def test_product_family_mapping_can_be_edited(settings):
    pack = RL.load_pack(settings)
    assert RL.products_for_skill(pack, "general") == ["home-loan", "personal-loan-unsecured", "multipurpose-loan"]

    RL.upsert_product(settings, "mccs", "home-loan", {"name": "สินเชื่อบ้าน", "skills": ["home-loan-advisor"]})
    pack = RL.load_pack(settings)
    assert pack.product("home-loan").skills == ["home-loan-advisor"]
    assert pack.product("home-loan").match  # untouched keys keep their value
    assert "home-loan" not in RL.products_for_skill(pack, "general")

    RL.upsert_product(settings, "mccs", "car-loan", {"name": "สินเชื่อรถยนต์", "skills": ["general"], "match": [r"สินเชื่อรถ"]})
    assert RL.detect_products(RL.load_pack(settings), "สนใจสินเชื่อรถยนต์") == ["car-loan"]
    RL.delete_product(settings, "mccs", "car-loan")
    assert RL.load_pack(settings).product("car-loan") is None

    with pytest.raises(ValueError, match="still used by"):
        RL.delete_product(settings, "mccs", "credit-card-bbl")
    with pytest.raises(ValueError, match="bad regex"):
        RL.upsert_product(settings, "mccs", "home-loan", {"name": "x", "match": ["("]})


def test_rule_can_be_written_by_hand(settings):
    rule = RL.create_rule(settings, "mccs", {
        "id": "internal-no-guarantees", "title": "ห้ามรับประกันผลอนุมัติ", "clause": "internal-1",
        "products": ["home-loan"], "check": "prohibited_phrase", "enforcement": "flag", "severity": "block",
        "phrases": ["รับรองว่าผ่านแน่นอน"], "system_rule": "ห้ามรับประกันว่าลูกค้าจะได้รับอนุมัติ",
    })
    assert rule.id == "internal-no-guarantees" and rule.path.exists()
    _, report = RL.guard(settings, "สินเชื่อบ้านนี้รับรองว่าผ่านแน่นอนค่ะ", language="th", skill_id="general")
    assert "internal-no-guarantees" in report["violations"]
    with pytest.raises(FileExistsError):
        RL.create_rule(settings, "mccs", {"id": "internal-no-guarantees", "products": ["home-loan"], "system_rule": "x"})
    with pytest.raises(ValueError, match="products is empty"):
        RL.create_rule(settings, "mccs", {"id": "nowhere", "system_rule": "x"})
    RL.delete_rule(settings, "mccs", "internal-no-guarantees")
    assert not any(r.id == "internal-no-guarantees" for r in RL.load_pack(settings).rules)


def test_rule_and_product_endpoints(tmp_path, monkeypatch):
    c = _api_client(tmp_path, monkeypatch)
    admin = ("admin", "secret")
    assert "credit-card" in c.get("/rules").json()["skills"]  # the picker's options

    assert c.put("/rules/products/home-loan", json={"name": "สินเชื่อบ้าน", "skills": ["general"]}).status_code == 401
    r = c.put("/rules/products/home-loan", json={"name": "สินเชื่อบ้าน", "skills": ["credit-card"]}, auth=admin)
    assert r.status_code == 200 and r.json()["skills"] == ["credit-card"]
    block = c.get("/rules/prompt", params={"skill": "credit-card"}).json()["block"]
    assert "สินเชื่อบ้าน" in block  # the family moved, so the credit-card agent now carries its rules
    assert c.delete("/rules/products/home-loan", auth=admin).status_code == 400  # still used by rules

    assert c.post("/rules", json={"id": "house-rule", "products": ["home-loan"], "system_rule": "x", "check": "judgement"}, auth=admin).status_code == 200
    assert c.post("/rules", json={"id": "house-rule", "products": ["home-loan"], "system_rule": "x"}, auth=admin).status_code == 409
    assert c.post("/rules", json={"id": "bad id!", "system_rule": "x"}, auth=admin).status_code == 400
    assert len(c.get("/rules").json()["rules"]) == 13
    assert c.delete("/rules/house-rule", auth=admin).status_code == 200
    assert c.delete("/rules/house-rule", auth=admin).status_code == 404
