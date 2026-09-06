"""Export / import of skills + knowledge + evals + pricing + runtime settings as one zip.

Export can be limited to parts (and PDFs left out, which is most of the size). Inspect compares a bundle with the
local files (new / changed / same) so the Studio can preview an import; import writes files and reports what changed
so the caller can re-ingest the touched categories and re-sync the skills."""
from __future__ import annotations

import hashlib
import io
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import Settings, overlay_path

Log = Callable[[str], None]
PARTS = ("skills", "knowledge", "evals", "config")
CONFIG_FILES = ("pricing.yaml", "settings.json")


def _base(settings: Settings, part: str) -> Path:
    return {"skills": settings.skills_dir, "knowledge": settings.knowledge_dir, "evals": settings.evals_dir}[part]


def _dest(settings: Settings, name: str) -> Optional[Path]:
    """Local path for a bundle entry, or None if the entry is not something we import."""
    p = Path(name)
    if not p.parts or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe path in bundle: {name}")
    top = p.parts[0]
    if top in ("skills", "knowledge", "evals") and len(p.parts) > 1:
        return _base(settings, top) / Path(*p.parts[1:])
    if name == "pricing.yaml":
        return settings.pricing_file
    if name == "settings.json":
        return overlay_path(settings.root)
    return None


def _part_of(name: str) -> str:
    top = Path(name).parts[0]
    return top if top in ("skills", "knowledge", "evals") else "config"


def export_bundle(settings: Settings, parts: Optional[Iterable[str]] = None, *, include_pdfs: bool = True) -> bytes:
    wanted = set(parts or PARTS)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in ("skills", "knowledge", "evals"):
            if part not in wanted:
                continue
            base = _base(settings, part)
            if not base.exists():
                continue
            for f in sorted(base.rglob("*")):
                if not f.is_file() or f.name.startswith("."):
                    continue
                if part == "knowledge" and not include_pdfs and f.suffix.lower() == ".pdf":
                    continue
                zf.write(f, f"{part}/{f.relative_to(base).as_posix()}")
        if "config" in wanted:
            for extra in (settings.pricing_file, overlay_path(settings.root)):
                if extra.exists():
                    zf.write(extra, "pricing.yaml" if extra.name == "pricing.yaml" else "settings.json")
    return buf.getvalue()


def inspect_bundle(settings: Settings, data: bytes) -> dict:
    """Preview: per-part counts and bytes, skills and knowledge categories inside, and new/changed/same vs local files."""
    out = {"parts": {p: {"files": 0, "bytes": 0, "new": 0, "changed": 0, "same": 0} for p in PARTS}, "skills": [], "categories": {}, "pdfs": 0, "skipped": [], "total_bytes": 0}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            n = info.filename
            dest = _dest(settings, n)
            if dest is None:
                out["skipped"].append(n)
                continue
            part = _part_of(n)
            st = out["parts"][part]
            st["files"] += 1
            st["bytes"] += info.file_size
            out["total_bytes"] += info.file_size
            parts = Path(n).parts
            if part == "skills" and len(parts) >= 3 and parts[-1] == "SKILL.md":
                out["skills"].append(parts[1])
            if part == "knowledge" and len(parts) >= 3:
                out["categories"][parts[1]] = out["categories"].get(parts[1], 0) + 1
                if n.lower().endswith(".pdf"):
                    out["pdfs"] += 1
            if not dest.exists():
                st["new"] += 1
            elif hashlib.sha256(dest.read_bytes()).hexdigest() == hashlib.sha256(zf.read(n)).hexdigest():
                st["same"] += 1
            else:
                st["changed"] += 1
    out["skills"].sort()
    return out


def import_bundle(settings: Settings, data: bytes, *, mode: str = "merge", parts: Optional[Iterable[str]] = None, log: Log = print) -> dict:
    """Write the bundle's files. Returns counts per part plus the skills and knowledge categories that were added or changed."""
    wanted = set(parts or PARTS)
    counts = {p: 0 for p in PARTS}
    changed_skills: set[str] = set()
    changed_categories: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        plan = []
        for n in names:
            dest = _dest(settings, n)  # validates the path
            if dest is None:
                log(f"skip {n}")
                continue
            if _part_of(n) in wanted:
                plan.append((n, dest))
        if mode == "replace":
            for part in ("skills", "knowledge"):
                d = _base(settings, part)
                if part in wanted and d.exists() and any(_part_of(n) == part for n, _ in plan):
                    shutil.rmtree(d)
                    log(f"removed existing {part}/")
        for n, dest in plan:
            part = _part_of(n)
            body = zf.read(n)
            same = dest.exists() and dest.read_bytes() == body
            if not same:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                p = Path(n).parts
                if part == "skills" and len(p) >= 2:
                    changed_skills.add(p[1])
                if part == "knowledge" and len(p) >= 3:
                    changed_categories.add(p[1])
            counts[part] += 1
    log(f"imported {counts}; changed skills: {sorted(changed_skills) or 'none'}; changed knowledge categories: {sorted(changed_categories) or 'none'}")
    return {**counts, "changed_skills": sorted(changed_skills), "changed_categories": sorted(changed_categories)}
