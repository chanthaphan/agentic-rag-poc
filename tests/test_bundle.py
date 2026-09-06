import io
import zipfile
from pathlib import Path

import pytest

from bankrag.bundle import export_bundle, import_bundle, inspect_bundle
from bankrag.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _dst(tmp_path):
    dst = Settings.load(ROOT)
    dst.root = tmp_path
    dst.state_dir = tmp_path / ".state"
    dst.skills_dir, dst.knowledge_dir, dst.evals_dir, dst.pricing_file = tmp_path / "skills", tmp_path / "knowledge", tmp_path / "evals", tmp_path / "pricing.yaml"
    return dst


def test_bundle_roundtrip_and_guard(tmp_path):
    src = Settings.load(ROOT)
    data = export_bundle(src)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "skills/credit-card/SKILL.md" in names and "pricing.yaml" in names and any(n.startswith("evals/") for n in names)
    dst = _dst(tmp_path)
    log = []
    res = import_bundle(dst, data, mode="merge", log=log.append)
    assert res["skills"] >= 6 and (tmp_path / "skills" / "credit-card" / "SKILL.md").exists() and (tmp_path / "pricing.yaml").exists()
    assert "credit-card" in res["changed_skills"] and "credit-card" in res["changed_categories"]
    # second import of the same bundle changes nothing
    res2 = import_bundle(dst, data, mode="merge", log=log.append)
    assert res2["changed_skills"] == [] and res2["changed_categories"] == []
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("skills/../evil.md", "x")
    with pytest.raises(ValueError):
        import_bundle(dst, bad.getvalue())


def test_selective_export_and_inspect(tmp_path):
    src = Settings.load(ROOT)
    skills_only = zipfile.ZipFile(io.BytesIO(export_bundle(src, ["skills"]))).namelist()
    assert skills_only and all(n.startswith("skills/") for n in skills_only)
    no_pdf = zipfile.ZipFile(io.BytesIO(export_bundle(src, ["knowledge"], include_pdfs=False))).namelist()
    assert no_pdf and not any(n.lower().endswith(".pdf") for n in no_pdf)
    dst = _dst(tmp_path)
    data = export_bundle(src, ["skills", "config"])
    info = inspect_bundle(dst, data)
    assert "credit-card" in info["skills"] and info["parts"]["skills"]["new"] == info["parts"]["skills"]["files"] and info["parts"]["knowledge"]["files"] == 0
    import_bundle(dst, data, log=lambda m: None)
    info2 = inspect_bundle(dst, data)
    assert info2["parts"]["skills"]["same"] == info2["parts"]["skills"]["files"] and info2["parts"]["skills"]["changed"] == 0
    # parts filter on import: knowledge in the bundle is ignored when not wanted
    res = import_bundle(dst, export_bundle(src, ["knowledge"], include_pdfs=False), parts=["skills"], log=lambda m: None)
    assert res["knowledge"] == 0 and not (tmp_path / "knowledge").exists()
