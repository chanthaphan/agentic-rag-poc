import io
import zipfile
from pathlib import Path

import pytest

from bankrag.skills import compose_instructions, install_skill_zip, load_base, load_skills, validate_skill

ROOT = Path(__file__).resolve().parents[1]


def test_load_repo_skills_are_valid():
    skills = load_skills(ROOT / "skills")
    assert {"credit-card", "debit-card", "insurance", "wealth", "general"} <= set(skills)
    for s in skills.values():
        errors, _ = validate_skill(s, ROOT / "knowledge")
        assert errors == [], (s.id, errors)
    assert skills["credit-card"].effective_filter == "product_category eq 'credit-card'"
    assert skills["general"].effective_filter == ""
    text = compose_instructions(load_base(ROOT / "skills"), skills["credit-card"])
    assert "knowledge_base_retrieve" in text and "# Skill: Credit Card Advisor" in text


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    return buf.getvalue()


def test_install_zip_root_and_nested(tmp_path):
    md = "---\nid: mortgage\nname: Mortgage\ndescription: home loans\nproduct_category: mortgage\nkeywords: [loan]\n---\nBody\n"
    spec = install_skill_zip(_zip({"SKILL.md": md, "notes.txt": "x"}), tmp_path)
    assert spec.id == "mortgage" and (tmp_path / "mortgage" / "notes.txt").exists()
    spec2 = install_skill_zip(_zip({"mortgage/SKILL.md": md}), tmp_path)
    assert spec2.name == "Mortgage"


def test_install_zip_rejects_traversal(tmp_path):
    md = "---\nid: bad\ndescription: x\n---\n"
    with pytest.raises(ValueError):
        install_skill_zip(_zip({"SKILL.md": md, "../evil.txt": "x"}), tmp_path)
