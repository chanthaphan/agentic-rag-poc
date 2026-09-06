from pathlib import Path

from bankrag.skills import lint_skills, load_skills, read_base, write_base

ROOT = Path(__file__).resolve().parents[1]


def test_lint_flags_overlap_and_missing_docs(tmp_path):
    skills = load_skills(ROOT / "skills")
    skills["credit-card"].keywords.append("ประกัน")  # duplicate of insurance
    res = lint_skills(skills, ROOT / "knowledge", deployed_models=["gpt-4.1-mini"], has_docs={"credit-card": True, "insurance": False, "wealth": False, "debit-card": False, "general": True})
    codes = {f["code"] for f in res["credit-card"]}
    assert "keyword-overlap" in codes and "insurance" in next(f["message"] for f in res["credit-card"] if f["code"] == "keyword-overlap")
    assert any(f["code"] == "no-documents" for f in res["insurance"])
    skills["wealth"].model = "gpt-9"
    res2 = lint_skills(skills, None, deployed_models=["gpt-4.1-mini"])
    assert any(f["code"] == "model-not-deployed" for f in res2["wealth"])


def test_base_read_write_guard(tmp_path):
    import shutil

    shutil.copytree(ROOT / "skills" / "_base", tmp_path / "_base")
    b = read_base(tmp_path)
    assert "knowledge base" in b["body"].lower()
    try:
        write_base(tmp_path, "just be nice")
        assert False, "should reject"
    except ValueError:
        pass
    out = write_base(tmp_path, b["body"] + "\n- Extra rule.")
    assert out["body"].endswith("- Extra rule.") and read_base(tmp_path)["frontmatter"].get("id") == "_base"
