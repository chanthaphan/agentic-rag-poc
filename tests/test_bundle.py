import io
import zipfile
from pathlib import Path

import pytest

from bankrag.bundle import export_bundle, import_bundle
from bankrag.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_bundle_roundtrip_and_guard(tmp_path):
    src = Settings.load(ROOT)
    data = export_bundle(src)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "skills/credit-card/SKILL.md" in names and "pricing.yaml" in names and any(n.startswith("evals/") for n in names)
    dst = Settings.load(ROOT)
    dst.root = tmp_path
    dst.state_dir = tmp_path / ".state"
    dst.skills_dir, dst.knowledge_dir, dst.evals_dir, dst.pricing_file = tmp_path / "skills", tmp_path / "knowledge", tmp_path / "evals", tmp_path / "pricing.yaml"
    log = []
    counts = import_bundle(dst, data, mode="merge", log=log.append)
    assert counts["skills"] >= 6 and (tmp_path / "skills" / "credit-card" / "SKILL.md").exists() and (tmp_path / "pricing.yaml").exists()
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("skills/../evil.md", "x")
    with pytest.raises(ValueError):
        import_bundle(dst, bad.getvalue())
