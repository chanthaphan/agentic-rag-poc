import base64
import json

from fastapi.testclient import TestClient

from bankrag import api, sessions as SESS


def _principal(name, email):
    claims = [{"typ": "name", "val": name}, {"typ": "preferred_username", "val": email}]
    return base64.b64encode(json.dumps({"auth_typ": "aad", "claims": claims}).encode()).decode()


def test_whoami_from_easy_auth_headers():
    c = TestClient(api.app)
    assert c.get("/whoami").json() == {"name": "", "email": "", "tester": "", "sso": False}
    r = c.get("/whoami", headers={"x-ms-client-principal": _principal("Pim Wong", "pim.wong@bangkokbank.com")}).json()
    assert r["name"] == "Pim Wong" and r["email"] == "pim.wong@bangkokbank.com" and r["sso"] is True
    r = c.get("/whoami", headers={"x-ms-client-principal-name": "kanit.m@bangkokbank.com"}).json()
    assert r["name"] == "Kanit M" and r["email"] == "kanit.m@bangkokbank.com"
    cfg = c.get("/app/config", headers={"x-ms-client-principal": _principal("Pim Wong", "pim.wong@bangkokbank.com")}).json()
    assert cfg["user_name"] == "Pim" and cfg["user_initials"] == "PW"


def test_session_records_who_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    s = api.settings
    from bankrag.models import Answer
    rec = SESS.new_record()
    rec.user_name, rec.user_email = "Pim Wong", "pim.wong@bangkokbank.com"
    SESS.append_turns(rec, "lounge?", Answer(skill_id="credit-card", confidence=0.9, text="2 visits", language="en"), by="Pim Wong")
    SESS.append_turns(rec, "fee?", Answer(skill_id="credit-card", confidence=0.9, text="3,000", language="en"))
    SESS.save_session(s, rec)
    rows = SESS.question_rows(s)
    assert [r["user"] for r in rows] == ["Pim Wong", "Pim Wong"]
    assert SESS.question_rows(s, user="Pim Wong") and SESS.question_rows(s, user="someone else") == []
    assert SESS.review_list(s)[0]["user_name"] == "Pim Wong" and SESS.known_users(s) == ["Pim Wong"]
    back = SESS.load_session(s, rec.id)
    assert back.user_email == "pim.wong@bangkokbank.com" and back.turns[0].by == "Pim Wong" and back.turns[2].by == "Pim Wong"


def test_app_history_is_per_person(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api.settings, "studio_admins", ["boss@bangkokbank.com"])
    SESS._schema_done.clear()
    s = api.settings
    from bankrag.models import Answer
    mine = SESS.new_record(); mine.user_name, mine.user_email = "Pim Wong", "pim.wong@bangkokbank.com"
    SESS.append_turns(mine, "q1", Answer(skill_id="credit-card", confidence=1, text="a", language="en")); SESS.save_session(s, mine)
    other = SESS.new_record(); other.user_name, other.user_email = "Kanit M", "kanit.m@bangkokbank.com"
    SESS.append_turns(other, "q2", Answer(skill_id="credit-card", confidence=1, text="b", language="en")); SESS.save_session(s, other)
    c = TestClient(api.app)
    pim = {"x-ms-client-principal": _principal("Pim Wong", "pim.wong@bangkokbank.com")}
    boss = {"x-ms-client-principal": _principal("Boss", "boss@bangkokbank.com")}
    assert [r["id"] for r in c.get("/sessions", headers=pim).json()] == [mine.id]
    assert c.get(f"/sessions/{mine.id}", headers=pim).status_code == 200
    assert c.get(f"/sessions/{other.id}", headers=pim).status_code == 403
    assert c.post(f"/chat/{other.id}/reset", headers=pim).status_code == 403
    # a Studio member sees everything: own list by default, all=1 for the whole list, any transcript
    assert len(c.get("/sessions?all=1", headers=boss).json()) == 2
    assert c.get(f"/sessions/{other.id}", headers=boss).status_code == 200
    # no SSO (local dev): unchanged, everything visible
    assert len(c.get("/sessions").json()) == 2
