import io
import zipfile

import frontmatter
from fastapi.testclient import TestClient

from bankrag import api

AUTH = ("tester", "secret")


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_dir", tmp_path / "skills")
    monkeypatch.setattr(api.settings, "knowledge_dir", tmp_path / "knowledge")
    monkeypatch.setattr(api.settings, "state_dir", tmp_path / ".state")
    monkeypatch.setattr(api.settings, "studio_password", "secret")
    (tmp_path / "skills").mkdir()
    return TestClient(api.app)


def test_studio_requires_password(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/studio", follow_redirects=False).status_code == 303  # -> login page
    assert c.get("/studio/login").status_code == 200
    assert c.post("/studio/login", data={"password": "nope"}, follow_redirects=False).headers["location"].endswith("error=1")
    r = c.post("/studio/login", data={"password": "secret", "tester": "Att"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/studio" and "bankrag_studio" in r.cookies and "bankrag_tester" in r.cookies
    c.cookies.clear()
    assert c.post("/skills", json={"id": "x"}).status_code == 401
    assert c.post("/skills", json={"id": "x"}, auth=("u", "wrong")).status_code == 401
    assert c.post("/knowledge/ingest", auth=("u", "wrong")).status_code == 401
    assert c.get("/studio", auth=AUTH).status_code == 200
    assert c.get("/studio?key=wrong", follow_redirects=False).headers["location"].endswith("error=1")
    r = c.get("/studio?key=secret", follow_redirects=False)
    assert r.status_code == 303 and "bankrag_studio" in r.cookies
    c.cookies.set("bankrag_studio", "secret")
    assert c.get("/studio").status_code == 200 and c.get("/knowledge/files?category=x").status_code == 200
    assert c.get("/").status_code == 200 and c.get("/legacy").status_code == 200
    assert c.get("/app/config").status_code == 200


def test_create_edit_delete_skill_and_files(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/skills", json={"id": "mortgage", "name": "Mortgage", "description": "home loans", "keywords": "loan, บ้าน"}, auth=AUTH)
    assert r.status_code == 200 and r.json()["spec"]["keywords"] == ["loan", "บ้าน"]
    assert c.post("/skills", json={"id": "mortgage"}, auth=AUTH).status_code == 409
    r = c.put("/skills/mortgage", json={"description": "Home loans and refinancing", "suggestions": "a\nb\nc", "top_k": 7, "body": "## Role\nBe helpful."}, auth=AUTH)
    assert r.status_code == 200 and r.json()["spec"]["top_k"] == 7 and r.json()["spec"]["suggestions"] == ["a", "b", "c"]
    post = frontmatter.load(tmp_path / "skills" / "mortgage" / "SKILL.md")
    assert list(post.metadata)[:4] == ["name", "id", "description", "product_category"] and post.content.strip() == "## Role\nBe helpful."
    got = c.get("/skills/mortgage").json()
    assert got["body"] == "## Role\nBe helpful." and got["frontmatter"]["description"] == "Home loans and refinancing"
    assert c.get("/skills/mortgage/zip", auth=AUTH).headers["content-type"] == "application/zip"
    # knowledge files
    r = c.post("/knowledge/upload?category=mortgage", files=[("files", ("rates.md", b"# Rates\n\n5%", "text/markdown"))], auth=AUTH)
    assert r.status_code == 200
    files = c.get("/knowledge/files?category=mortgage", auth=AUTH).json()
    assert files[0]["path"] == "mortgage/_uploads/rates.md" and files[0]["indexed"] is False
    assert c.delete("/knowledge/files?path=../../etc/passwd", auth=AUTH).status_code == 400
    assert c.delete("/knowledge/files?path=mortgage/_uploads/rates.md", auth=AUTH).status_code == 200
    assert c.get("/knowledge/files?category=mortgage", auth=AUTH).json() == []
    r = c.delete("/skills/mortgage?prune=false", auth=AUTH)
    assert r.status_code == 200 and not (tmp_path / "skills" / "mortgage").exists()


def test_chat_persists_and_rehydrates(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    import shutil
    from pathlib import Path

    shutil.copytree(Path(__file__).resolve().parents[1] / "skills", tmp_path / "skills", dirs_exist_ok=True)
    seen = {}

    class StubSession:
        def __init__(self, settings, skills, project=None):
            self.settings, self.skills, self.conversation_id, self.history, self.prev_skill = settings, skills, None, [], None

        @classmethod
        def from_record(cls, settings, skills, rec, project=None):
            s = cls(settings, skills)
            s.conversation_id, s.prev_skill = rec.conversation_id, rec.prev_skill
            s.history = [{"role": t.role, "content": t.text} for t in rec.turns]
            seen["rehydrated_history"] = len(s.history)
            return s

        def to_record(self, rec):
            rec.conversation_id, rec.prev_skill = self.conversation_id, self.prev_skill
            return rec

        def ask(self, q, force_skill=None, with_sources=True):
            self.conversation_id = self.conversation_id or "conv_stub"
            self.history += [{"role": "user", "content": q}, {"role": "assistant", "content": "ok"}]
            self.prev_skill = "credit-card"
            from bankrag.models import Answer

            return Answer(skill_id="credit-card", confidence=0.9, text="ok", suggestions=["x"])

        def reset(self):
            pass

    import bankrag.chat as chat_mod

    monkeypatch.setattr(chat_mod, "ChatSession", StubSession)
    r = c.post("/chat", json={"message": "hello"}).json()
    sid = r["session_id"]
    assert r["answer"]["suggestions"] == ["x"] and r["title"] == "hello"
    api._sessions.clear()  # simulate restart
    r2 = c.post("/chat", json={"session_id": sid, "message": "follow up"}).json()
    assert r2["session_id"] == sid and seen["rehydrated_history"] == 2
    rec = c.get(f"/sessions/{sid}").json()
    assert len(rec["turns"]) == 4 and rec["conversation_id"] == "conv_stub" and rec["prev_skill"] == "credit-card"
    assert c.get("/sessions").json()[0]["id"] == sid
    import base64
    pw = {"Authorization": "Basic " + base64.b64encode(f"x:{api.settings.studio_password}".encode()).decode()}
    assert c.delete(f"/sessions/{sid}").status_code in (401, 403)  # deleting conversations is admin-only now
    assert c.delete(f"/sessions/{sid}", headers=pw).json()["ok"] is True
