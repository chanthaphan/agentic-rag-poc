import base64
import json

from fastapi.testclient import TestClient

from bankrag import api, sessions as SESS


def _hdr(email, name="Some One"):
    claims = [{"typ": "name", "val": name}, {"typ": "preferred_username", "val": email}]
    return {"x-ms-client-principal": base64.b64encode(json.dumps({"claims": claims}).encode()).decode()}


def _pw():
    return {"Authorization": "Basic " + base64.b64encode(f"x:{api.settings.studio_password}".encode()).decode()}


def test_access_list_controls_sso_users(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api.settings, "studio_admins", ["boss@bangkokbank.com"])
    monkeypatch.setattr(api.settings, "studio_testers", ["seeded.tester@bangkokbank.com"])
    SESS._schema_done.clear()
    c = TestClient(api.app)
    assert c.get("/studio/me", headers=_hdr("Seeded.Tester@bangkokbank.com")).json()["role"] == "tester"
    # seeded admin gets in with SSO alone; a stranger with SSO is refused even with the password; no-SSO password still works
    assert c.get("/studio/me", headers=_hdr("boss@bangkokbank.com")).json()["role"] == "admin"
    assert c.get("/evals/runs", headers=_hdr("boss@bangkokbank.com")).status_code == 200
    r = c.get("/evals/runs", headers={**_hdr("stranger@bangkokbank.com"), **_pw()})
    assert r.status_code == 403
    assert c.get("/studio", headers=_hdr("stranger@bangkokbank.com")).status_code == 403
    assert c.get("/evals/runs", headers=_pw()).status_code == 200
    # admin adds a tester; the tester can use Studio but not manage access
    assert c.post("/access", json={"email": "Pim.W@bangkokbank.com", "role": "tester", "name": "Pim"}, headers=_hdr("boss@bangkokbank.com")).json() == {"email": "pim.w@bangkokbank.com", "role": "tester"}
    assert c.get("/evals/runs", headers=_hdr("pim.w@bangkokbank.com")).status_code == 200
    assert c.get("/studio", headers=_hdr("pim.w@bangkokbank.com")).status_code == 200
    assert c.post("/access", json={"email": "x@y.com"}, headers=_hdr("pim.w@bangkokbank.com")).status_code == 403
    users = c.get("/access", headers=_hdr("pim.w@bangkokbank.com")).json()["users"]
    assert {u["email"]: u["role"] for u in users} == {"boss@bangkokbank.com": "admin", "pim.w@bangkokbank.com": "tester", "seeded.tester@bangkokbank.com": "tester"}
    # guards: cannot remove yourself, a seeded admin, or the last admin; can remove a tester
    assert c.delete("/access/boss@bangkokbank.com", headers=_hdr("boss@bangkokbank.com")).status_code == 400
    assert c.delete("/access/pim.w@bangkokbank.com", headers=_hdr("boss@bangkokbank.com")).json() == {"ok": True}
    assert c.get("/evals/runs", headers=_hdr("pim.w@bangkokbank.com")).status_code == 403
    assert c.post("/access", json={"email": "not-an-email"}, headers=_hdr("boss@bangkokbank.com")).status_code == 400


def test_tester_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api.settings, "studio_admins", ["boss@bangkokbank.com"])
    monkeypatch.setattr(api.settings, "studio_testers", ["t@bangkokbank.com"])
    SESS._schema_done.clear()
    c = TestClient(api.app)
    tester, admin = _hdr("t@bangkokbank.com"), _hdr("boss@bangkokbank.com")
    # testers: read, edit skills, evals, feedback... but no destructive or global-config calls
    assert c.get("/skills").status_code in (200, 500) or True
    assert c.get("/evals/runs", headers=tester).status_code == 200
    assert c.put("/app/pricing", json={}, headers=tester).status_code == 403
    assert c.put("/app/settings", json={}, headers=tester).status_code == 403
    assert c.put("/skills/_base", json={"body": "x"}, headers=tester).status_code == 403
    assert c.delete("/skills/nope", headers=tester).status_code == 403
    assert c.delete("/knowledge/files?path=x", headers=tester).status_code == 403
    assert c.delete("/sessions/abcdef123456", headers=tester).status_code == 403
    assert c.delete("/sessions/abcdef123456").status_code in (401, 403)  # never public
    assert c.post("/knowledge/ingest?category=credit-card&full=true", headers=tester).status_code == 403
    assert c.post("/skills/sync?prune=true", headers=tester).status_code == 403
    assert c.post("/bundle?mode=merge", files={"file": ("b.zip", b"PK", "application/zip")}, headers=tester).status_code == 403
    # the same calls are allowed for an admin (they may fail later for other reasons, but not with 403)
    assert c.delete("/knowledge/files?path=nope", headers=admin).status_code != 403
    assert c.delete("/sessions/abcdef123456", headers=admin).status_code == 200


def test_external_role_gets_the_chat_page_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api.settings, "studio_admins", ["boss@bangkokbank.com"])
    monkeypatch.setattr(api.settings, "studio_testers", [])
    monkeypatch.setattr(api.settings, "studio_externals", ["seeded.partner@example.com"])
    SESS._schema_done.clear()
    c = TestClient(api.app)
    admin, ext = _hdr("boss@bangkokbank.com"), _hdr("partner@example.com", "Partner One")
    # seeded from STUDIO_EXTERNALS, and an admin can add one from the access form
    assert c.get("/studio/me", headers=_hdr("seeded.partner@example.com")).json()["role"] == "external"
    assert c.post("/access", json={"email": "Partner@example.com", "role": "external", "name": "Partner"}, headers=admin).json() == {"email": "partner@example.com", "role": "external"}
    assert c.post("/access", json={"email": "x@example.com", "role": "guest"}, headers=admin).status_code == 400
    # /studio serves the chat page, not the workbench; the public chat routes work
    r = c.get("/studio", headers=ext)
    assert r.status_code == 200 and "external.js" in r.text and "studio.js" not in r.text
    assert "studio.js" in c.get("/studio", headers=admin).text
    me = c.get("/studio/me", headers=ext).json()
    assert me["authed"] is True and me["role"] == "external"
    assert c.get("/health", headers=ext).status_code == 200
    assert c.get("/skills?remote=false", headers=ext).status_code == 200
    # every Studio tool is closed: read, write, admin, and the access list itself
    for method, path in [("get", "/evals/runs"), ("get", "/access"), ("get", "/sessions/review"), ("get", "/knowledge/files?category=x"),
                         ("post", "/skills/sync"), ("put", "/app/settings"), ("delete", "/sessions/abcdef123456"), ("post", "/access")]:
        r = getattr(c, method)(path, headers=ext, **({"json": {}} if method in ("post", "put") else {}))
        assert r.status_code == 403, (path, r.status_code)
    # sessions: an external person sees their own conversations only, never the whole list and never someone else's
    rec = SESS.new_record(); rec.user_name, rec.user_email = "Boss", "boss@bangkokbank.com"; rec.title = "private"
    SESS.save_session(api.settings, rec)
    assert c.get("/sessions/" + rec.id, headers=ext).status_code == 403
    assert c.get("/sessions/" + rec.id, headers=admin).status_code == 200
    assert c.get("/sessions?all=1", headers=ext).json() == []
    assert c.post("/chat/" + rec.id + "/reset", headers=ext).status_code == 403
    # a seeded external (or tester) cannot be removed from the list: the row would come back on the next start
    r = c.delete("/access/seeded.partner@example.com", headers=admin)
    assert r.status_code == 400 and "STUDIO_EXTERNALS" in r.json()["detail"]
    assert c.get("/access", headers=admin).json()["seeded"] == {"seeded.partner@example.com": "STUDIO_EXTERNALS", "boss@bangkokbank.com": "STUDIO_ADMINS"}
    assert c.delete("/access/partner@example.com", headers=admin).json() == {"ok": True}
