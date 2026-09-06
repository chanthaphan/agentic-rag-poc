"""Export / import of skills + knowledge + evals + pricing + runtime settings as one zip."""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path
from typing import Callable

from .config import Settings, overlay_path

Log = Callable[[str], None]
PARTS = ("skills", "knowledge", "evals")


def export_bundle(settings: Settings) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in PARTS:
            base = {"skills": settings.skills_dir, "knowledge": settings.knowledge_dir, "evals": settings.evals_dir}[part]
            if not base.exists():
                continue
            for f in sorted(base.rglob("*")):
                if f.is_file() and not f.name.startswith("."):
                    zf.write(f, f"{part}/{f.relative_to(base).as_posix()}")
        for extra in (settings.pricing_file, overlay_path(settings.root)):
            if extra.exists():
                zf.write(extra, "pricing.yaml" if extra.name == "pricing.yaml" else "settings.json")
    return buf.getvalue()


def import_bundle(settings: Settings, data: bytes, *, mode: str = "merge", log: Log = print) -> dict[str, int]:
    counts = {p: 0 for p in PARTS}
    counts["other"] = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        for n in names:
            parts = Path(n).parts
            if not parts or Path(n).is_absolute() or ".." in parts:
                raise ValueError(f"unsafe path in bundle: {n}")
        if mode == "replace":
            for part in ("skills", "knowledge"):
                d = {"skills": settings.skills_dir, "knowledge": settings.knowledge_dir}[part]
                if d.exists() and any(n.startswith(part + "/") for n in names):
                    shutil.rmtree(d)
                    log(f"removed existing {part}/")
        for n in names:
            top = Path(n).parts[0]
            if top in PARTS:
                base = {"skills": settings.skills_dir, "knowledge": settings.knowledge_dir, "evals": settings.evals_dir}[top]
                dest = base / Path(*Path(n).parts[1:])
                counts[top] += 1
            elif n == "pricing.yaml":
                dest = settings.pricing_file
                counts["other"] += 1
            elif n == "settings.json":
                dest = overlay_path(settings.root)
                counts["other"] += 1
            else:
                log(f"skip {n}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(n))
    log(f"imported {counts}")
    return counts
