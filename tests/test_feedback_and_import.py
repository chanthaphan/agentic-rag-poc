from pathlib import Path

from fastapi.testclient import TestClient

from bankrag import api
from bankrag.ingest.import_url import _html_to_markdown, _slug
from bankrag.models import Answer
from bankrag.sessions import append_turns, new_record, save_session

ROOT = Path(__file__).resolve().parents[1]


def test_feedback_roundtrip_and_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "state_dir", tmp_path / ".state")
    monkeypatch.setattr(api.settings, "studio_password", "secret")
    rec = new_record()
    append_turns(rec, "q1", Answer(skill_id="credit-card", confidence=0.9, text="a1", trace={"cost": {"total_usd": 0.01}}))
    save_session(api.settings, rec)
    c = TestClient(api.app)
    c.cookies.set("bankrag_tester", "Att")
    r = c.post("/feedback", json={"session_id": rec.id, "idx": 1, "rating": "down", "comment": "wrong fee"})
    assert r.status_code == 200 and r.json()["tester"] == "Att"
    assert c.get(f"/feedback?session_id={rec.id}").json()[0]["rating"] == "down"
    assert c.post("/feedback", json={"session_id": rec.id, "idx": 1, "rating": "meh"}).status_code == 400
    c.cookies.set("bankrag_studio", "secret")
    review = c.get("/sessions/review").json()
    assert review[0]["id"] == rec.id and review[0]["down"] == 1 and review[0]["skills"] == ["credit-card"]
    assert c.get("/sessions/review?rating=up").json() == []
    csv = c.get("/feedback.csv").text
    assert "wrong fee" in csv and "q1" in csv and "credit-card" in csv
    assert c.get("/knowledge/chunks?path=../x").status_code == 400
    assert c.get("/evals/routing").status_code == 200 and c.get("/evals/nope").status_code == 404


def test_import_helpers():
    assert _slug("https://www.bangkokbank.com/th-TH/Personal/Cards/Debit-Cards/Be1st-Smart") == "be1st-smart"
    md, title = _html_to_markdown("<html><head><title>Be1st  Smart</title></head><body><nav>x</nav><h1>Card</h1><p>Fee <b>200</b> baht</p></body></html>")
    assert title == "Be1st Smart" and "200" in md and "nav" not in md.lower().replace("x", "")
