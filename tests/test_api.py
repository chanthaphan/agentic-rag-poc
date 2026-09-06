import io
import zipfile

from fastapi.testclient import TestClient

from bankrag import api


def test_health_and_skills_without_azure(tmp_path, monkeypatch):
    client = TestClient(api.app)
    assert client.get("/health").json()["ok"] is True
    rows = client.get("/skills?remote=false").json()
    assert {r["id"] for r in rows} >= {"credit-card", "general"}
    one = client.get("/skills/credit-card").json()
    assert "knowledge_base_retrieve" not in one["skill_md"]  # base rules live in _base, not in the skill file
    assert one["spec"]["product_category"] == "credit-card"
    assert client.get("/").status_code == 200


def test_upload_skill_zip_and_knowledge_file(tmp_path, monkeypatch):
    monkeypatch.setattr(api.settings, "skills_dir", tmp_path / "skills")
    monkeypatch.setattr(api.settings, "knowledge_dir", tmp_path / "knowledge")
    monkeypatch.setattr(api.settings, "studio_password", "pw")
    (tmp_path / "skills").mkdir()
    client = TestClient(api.app)
    auth = ("t", "pw")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nid: mortgage\nname: Mortgage\ndescription: home loans\nproduct_category: mortgage\nkeywords: [loan]\n---\nBody\n")
    r = client.post("/skills/upload", files={"file": ("mortgage.zip", buf.getvalue(), "application/zip")}, auth=auth)
    assert r.status_code == 200 and r.json()["id"] == "mortgage"
    r = client.post("/knowledge/upload?category=mortgage", files=[("files", ("rates.md", b"# Rates\n\n5%", "text/markdown"))], auth=auth)
    assert r.status_code == 200 and r.json()["saved"] == ["mortgage/_uploads/rates.md"]
    stats = client.get("/knowledge/stats").json()
    assert any(row["category"] == "mortgage" and row["files"] == 1 for row in stats["rows"])
    r = client.post("/skills/upload", files={"file": ("bad.zip", b"notazip", "application/zip")}, auth=auth)
    assert r.status_code == 400
