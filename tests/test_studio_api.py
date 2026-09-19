import io
import json
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


def test_an_llm_key_can_be_bound_from_outside_and_is_never_handed_back(tmp_path, monkeypatch):
    """A team can point the POC at their own model service from Studio, without touching the image or .env.

    The key itself is write-only: it is stored on the server, reported as dots, and the dots coming back must not
    overwrite it - otherwise saving any other setting would quietly wipe the binding."""
    from bankrag import llm as L

    c = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(api.settings, "root", tmp_path)
    # saving reloads Settings from disk, which is the point of the feature and also drops the test's password
    def save(body):
        out = c.put("/app/settings", json=body, auth=AUTH)
        monkeypatch.setattr(api.settings, "studio_password", "secret")
        monkeypatch.setattr(api.settings, "root", tmp_path)
        return out

    r = save({"LLM_PROVIDER": "openai", "LLM_BASE_URL": "https://api.openai.com/v1/", "LLM_API_KEY": "sk-secret-value"})
    assert r.status_code == 200 and r.json()["overlay"]["LLM_API_KEY"] == api.MASK

    res = c.get("/app/settings", auth=AUTH)
    assert res.status_code == 200, res.text[:200]
    got = res.json()
    assert got["effective"]["LLM_PROVIDER"] == "openai"
    assert got["effective"]["LLM_BASE_URL"] == "https://api.openai.com/v1"
    assert got["effective"]["LLM_API_KEY"] == api.MASK and "sk-secret-value" not in json.dumps(got)
    assert api.settings.uses_own_service and api.settings.model_key == "sk-secret-value"

    # the page sends the mask back when it saves something else: the key survives
    save({"LLM_API_KEY": api.MASK, "REALTIME_VOICE": "marin"})
    assert api.settings.model_key == "sk-secret-value" and api.settings.realtime_voice == "marin"

    # the models are then built against that service, with that key
    model = L.chat_model(api.settings, "gpt-4.1-mini")
    assert type(model).__name__ == "ChatOpenAI"
    assert str(model.openai_api_base).rstrip("/") == "https://api.openai.com/v1"
    from bankrag import realtime as RT
    assert RT.auth_headers(api.settings) == {"Authorization": "Bearer sk-secret-value"}

    # the test button reports the failure rather than throwing it
    def boom(settings, m, **kw):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(L, "chat_model", boom)
    out = c.post("/app/llm/test", json={}, auth=AUTH).json()
    assert out["ok"] is False and "401" in out["error"] and out["service"] == "openai"

    # clearing the box removes the binding and the app falls back to the Azure account
    save({"LLM_API_KEY": "", "LLM_PROVIDER": "azure", "LLM_BASE_URL": ""})
    assert not api.settings.uses_own_service and api.settings.llm_api_key == ""


def test_a_key_can_come_from_any_of_the_four_services(tmp_path, monkeypatch):
    """OpenAI, Azure OpenAI, an OpenAI-compatible gateway (LiteLLM and friends) or Anthropic.

    Speech mode is the exception: realtime and audio models exist only on Azure OpenAI and OpenAI, so a gateway or
    Claude answers the chat while the voice stays on the Azure account the app was deployed with."""
    from bankrag import llm as L, realtime as RT
    from bankrag.config import Settings

    s = Settings.load(tmp_path)
    s.aoai_endpoint, s.aoai_api_key = "https://acct.openai.azure.com", "azure-key"

    s.llm_provider, s.llm_base_url, s.llm_api_key = "azure", "", ""
    assert not s.uses_own_service and s.speech_service == "azure"
    assert type(L.chat_model(s, "gpt-4.1-mini")).__name__ == "AzureChatOpenAI"
    assert RT.auth_headers(s) == {"api-key": "azure-key"}

    s.llm_provider, s.llm_base_url, s.llm_api_key = "openai", "", "sk-openai"
    assert s.chat_base_url == "https://api.openai.com/v1" and s.speech_service == "openai"
    assert type(L.chat_model(s, "gpt-4.1-mini")).__name__ == "ChatOpenAI"
    assert RT.auth_headers(s) == {"Authorization": "Bearer sk-openai"}
    assert s.aoai_v1_base_url == "https://api.openai.com/v1"

    s.llm_provider, s.llm_base_url, s.llm_api_key = "compatible", "https://litellm.mybank.local/v1", "sk-lite"
    assert s.uses_own_service and type(L.chat_model(s, "claude-sonnet-4")).__name__ == "ChatOpenAI"
    assert s.speech_service == "azure"                      # the gateway has no realtime models
    assert RT.auth_headers(s) == {"api-key": "azure-key"}
    assert s.aoai_v1_base_url == "https://acct.openai.azure.com/openai/v1"

    s.llm_provider, s.llm_base_url, s.llm_api_key = "anthropic", "", "sk-ant-key"
    assert s.uses_own_service and s.speech_service == "azure"
    assert type(L.chat_model(s, "claude-sonnet-4-5")).__name__ == "ChatAnthropic"

    # the names a team actually types are accepted
    from bankrag.config import _provider
    assert _provider("litellm") == "compatible" and _provider("openrouter") == "compatible"
    assert _provider("claude") == "anthropic" and _provider("nonsense") == "azure"
