"""Ingest knowledge/<category>/** documents into the search index (incremental by content hash)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import frontmatter
import yaml

from ..config import Settings
from ..models import Chunk, IngestDocReport, IngestReport
from . import clean as C
from .chunk import chunk_markdown
from .embed import embed_texts
from .progress import report as progress

MANIFEST = "ingest_manifest.json"
IGNORED_NAMES = {"README.md", "readme.md", "doc.yaml", ".DS_Store"}
Log = Callable[[str], None]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _chunk_id(doc_id: str, idx: int) -> str:
    return hashlib.sha1(f"{doc_id}#{idx}".encode("utf-8")).hexdigest()


def load_manifest(settings: Settings) -> dict:
    p = settings.state_dir / MANIFEST
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"docs": {}}


def save_manifest(settings: Settings, manifest: dict) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    p = settings.state_dir / MANIFEST
    fd, tmp = tempfile.mkstemp(dir=settings.state_dir, prefix=".manifest-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def discover(knowledge_dir: Path, category: Optional[str] = None) -> list[tuple[str, Path]]:
    """Return [(category, path)] for every .md/.pdf under knowledge/<category>/."""
    out: list[tuple[str, Path]] = []
    if not knowledge_dir.exists():
        return out
    for cat_dir in sorted(knowledge_dir.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith((".", "_")):
            continue
        if category and cat_dir.name != category:
            continue
        for p in sorted(cat_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".md", ".pdf") and p.name not in IGNORED_NAMES:
                if p.suffix.lower() == ".pdf" and p.with_suffix(".md").exists():
                    continue  # pre-extracted text exists next to the pdf; ingest the .md only
                out.append((cat_dir.name, p))
    return out


def _derive_doc_type(rel: Path, ext: str) -> str:
    parts = {x.lower() for x in rel.parts}
    if "_promotions" in parts or "promotions" in parts:
        return "promotion"
    if "_terms" in parts or "termsandconditions" in parts:
        return "terms"
    if ext == ".pdf" or "pdfs" in parts:
        return "pdf"
    return "product-page"


def _read_document(category: str, path: Path, knowledge_dir: Path) -> tuple[str, dict]:
    """Return (markdown_text, metadata) applying precedence frontmatter > doc.yaml > derived."""
    rel = path.relative_to(knowledge_dir / category)
    meta: dict = {}
    sidecar = path.with_suffix(".yaml") if path.suffix.lower() == ".pdf" else None
    for cand in [path.parent / "doc.yaml", sidecar]:
        if cand and cand.exists():
            try:
                data = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    # doc.yaml may be shared by a folder or keyed by filename
                    meta.update(data.get(path.name, {}) if path.name in data else {k: v for k, v in data.items() if not isinstance(v, dict)})
            except Exception:
                pass
    if path.suffix.lower() == ".pdf":
        from .pdf_text import extract  # lazy: pymupdf import

        res = extract(str(path))
        text = res.text or ""
        if getattr(res, "image_based", False) and not text.strip():
            meta["_skip"] = "image-only PDF (no text layer); OCR not enabled"
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
        post = frontmatter.loads(raw)
        meta.update({k: v for k, v in post.metadata.items()})
        comment_meta = C.extract_meta(post.content)
        meta.setdefault("source_url", comment_meta.get("url", ""))
        meta.setdefault("title", comment_meta.get("title", ""))
        text = post.content
    cleaned = C.clean_markdown(text, cut_footer=bool(meta.get("cut_footer", True)))
    title = str(meta.get("title") or C.first_heading(cleaned) or path.stem)
    product_name = str(meta.get("product_name") or (rel.parts[0] if len(rel.parts) > 1 and not rel.parts[0].startswith("_") else ""))
    doc_type = str(meta.get("doc_type") or _derive_doc_type(rel, path.suffix.lower()))
    source_url = str(meta.get("source_url") or f"bankrag://{category}/{rel.as_posix()}")
    meta.update({"title": title, "product_name": product_name, "doc_type": doc_type, "source_url": source_url, "rel": rel.as_posix()})
    return cleaned, meta


def build_chunks(category: str, doc_id: str, text: str, meta: dict, content_hash: str) -> list[Chunk]:
    now = datetime.now(timezone.utc).isoformat()
    lang = C.detect_language(text)
    out: list[Chunk] = []
    for c in chunk_markdown(text, meta["title"]):
        out.append(
            Chunk(
                id=_chunk_id(doc_id, c["chunk_index"]),
                doc_id=doc_id,
                content=c["content"],
                title=meta["title"],
                breadcrumb=c["breadcrumb"],
                product_category=category,
                product_name=meta["product_name"],
                doc_type=meta["doc_type"],
                source_url=meta["source_url"],
                source_file=meta["rel"],
                chunk_index=c["chunk_index"],
                language=lang,
                content_hash=content_hash,
                last_updated=now,
            )
        )
    return out


def ingest(settings: Settings, *, category: Optional[str] = None, full: bool = False, dry_run: bool = False, only_paths: Optional[list[str]] = None, log: Log = print) -> IngestReport:
    from .. import search_index as SI  # lazy import (azure sdk)

    settings.ensure_dirs()
    manifest = load_manifest(settings)
    docs_state: dict = manifest.setdefault("docs", {})
    report = IngestReport(dry_run=dry_run)
    found = discover(settings.knowledge_dir, category)
    seen_ids: set[str] = set()
    pending: list[Chunk] = []
    to_delete: list[str] = []

    only = set(only_paths or [])
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "deleted": 0, "chunks": 0}
    targets = [(c, p) for c, p in found if not only or p.relative_to(settings.knowledge_dir).as_posix() in only]
    log(f"scanning {len(targets)} file(s) in {category or 'all categories'}{' (full re-ingest)' if full else ''}")
    progress(log, "scan", 0, len(targets), message="", **counts)
    scanned = 0
    for cat, path in found:
        rel = path.relative_to(settings.knowledge_dir).as_posix()
        doc_id = rel
        seen_ids.add(doc_id)
        if only and rel not in only:
            continue
        scanned += 1
        progress(log, "scan", scanned, len(targets), message=rel, **counts)
        content_hash = _sha256(path.read_bytes())
        prev = docs_state.get(doc_id)
        if prev and prev.get("sha256") == content_hash and not full:
            report.docs.append(IngestDocReport(doc_id=doc_id, action="unchanged", chunks=len(prev.get("chunk_ids", []))))
            counts["unchanged"] += 1
            continue
        try:
            text, meta = _read_document(cat, path, settings.knowledge_dir)
        except Exception as e:  # noqa: BLE001
            report.docs.append(IngestDocReport(doc_id=doc_id, action="skipped", note=f"read error: {e}"))
            counts["skipped"] += 1
            log(f"skipped {rel}: read error: {str(e)[:120]}")
            continue
        if meta.get("_skip") or not text.strip():
            note = str(meta.get("_skip") or "empty after cleaning")
            report.docs.append(IngestDocReport(doc_id=doc_id, action="skipped", note=note))
            counts["skipped"] += 1
            log(f"skipped {rel}: {note}")
            continue
        chunks = build_chunks(cat, doc_id, text, meta, content_hash)
        new_ids = [c.id for c in chunks]
        old_ids = set(prev.get("chunk_ids", [])) if prev else set()
        to_delete.extend(sorted(old_ids - set(new_ids)))
        pending.extend(chunks)
        docs_state[doc_id] = {"sha256": content_hash, "chunk_ids": new_ids, "category": cat, "title": meta["title"], "doc_type": meta["doc_type"]}
        action = "updated" if prev else "added"
        report.docs.append(IngestDocReport(doc_id=doc_id, action=action, chunks=len(chunks), note=meta["title"][:60]))
        report.per_category[cat] = report.per_category.get(cat, 0) + len(chunks)
        counts[action] += 1
        counts["chunks"] += len(chunks)
        log(f"{action} {rel} -> {len(chunks)} chunks ({meta['title'][:50]})")

    # documents removed from disk (within the selected category scope)
    for doc_id, st in list(docs_state.items()):
        if doc_id in seen_ids or only:
            continue
        if category and st.get("category") != category:
            continue
        to_delete.extend(st.get("chunk_ids", []))
        report.docs.append(IngestDocReport(doc_id=doc_id, action="deleted", chunks=len(st.get("chunk_ids", []))))
        counts["deleted"] += 1
        log(f"deleted {doc_id} (file removed from disk; {len(st.get('chunk_ids', []))} chunks will be dropped)")
        del docs_state[doc_id]

    progress(log, "scan", len(targets), len(targets), message="", **counts)
    log(f"documents: {report.summary()}  new/updated chunks: {len(pending)}  chunks to delete: {len(to_delete)}")
    if dry_run:
        return report

    if pending:
        log(f"embedding {len(pending)} chunks with {settings.embed_deployment} ...")
        progress(log, "embed", 0, len(pending), message=settings.embed_deployment)

        def on_embed(done: int, total: int) -> None:
            progress(log, "embed", done, total)
            if done % 128 == 0 or done == total:
                log(f"embedded {done}/{total}")

        vectors = embed_texts([c.content for c in pending], settings, on_progress=on_embed)
        docs = []
        for c, v in zip(pending, vectors):
            d = c.model_dump()
            d["content_vector"] = v
            docs.append(d)
        log("uploading to index ...")
        progress(log, "upload", 0, len(docs), message="")
        report.uploaded_chunks = SI.upload_chunks(settings, docs, on_progress=lambda d, n: progress(log, "upload", d, n))
    if to_delete:
        progress(log, "upload", len(to_delete), len(to_delete), message=f"deleting {len(to_delete)} old chunks")
        report.deleted_chunks = SI.delete_chunks(settings, to_delete)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(settings, manifest)
    counts["uploaded"], counts["deleted_chunks"] = report.uploaded_chunks, report.deleted_chunks
    progress(log, "done", None, None, message="", **counts)
    log(f"uploaded {report.uploaded_chunks}, deleted {report.deleted_chunks}; manifest saved")
    return report


def local_counts(settings: Settings) -> dict[str, dict[str, int]]:
    """Files on disk and indexed chunks (from manifest) per category."""
    out: dict[str, dict[str, int]] = {}
    for cat, _ in discover(settings.knowledge_dir):
        out.setdefault(cat, {"files": 0, "chunks": 0})["files"] += 1
    for doc_id, st in load_manifest(settings).get("docs", {}).items():
        out.setdefault(st.get("category", "?"), {"files": 0, "chunks": 0})["chunks"] += len(st.get("chunk_ids", []))
    return out


def pdf_status(settings: Settings, path: Path) -> dict:
    """Text-layer status of a PDF, cached by sha256 in .state/pdf_status.json."""
    cache_p = settings.state_dir / "pdf_status.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    h = _sha256(path.read_bytes())
    if h in cache:
        return cache[h]
    try:
        from .pdf_text import extract

        res = extract(str(path))
        info = {"text_chars": len(res.text or ""), "image_based": bool(getattr(res, "image_based", False)), "pages": getattr(res, "pages", None)}
    except Exception as e:  # noqa: BLE001
        info = {"text_chars": 0, "image_based": None, "error": str(e)[:120]}
    cache[h] = info
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    cache_p.write_text(json.dumps(cache), encoding="utf-8")
    return info


def list_knowledge_files(settings: Settings, category: str, with_pdf_status: bool = True) -> list[dict]:
    """Files under knowledge/<category>/ with size, indexed chunk count from the manifest, and PDF text status."""
    docs = load_manifest(settings).get("docs", {})
    out: list[dict] = []
    for cat, path in discover(settings.knowledge_dir, category):
        rel = path.relative_to(settings.knowledge_dir).as_posix()
        st = docs.get(rel, {})
        row = {"path": rel, "kind": "pdf" if path.suffix.lower() == ".pdf" else "md", "size": path.stat().st_size, "chunks": len(st.get("chunk_ids", [])), "title": st.get("title", ""), "indexed": bool(st)}
        if row["kind"] == "pdf" and with_pdf_status:
            row.update(pdf_status(settings, path))
        out.append(row)
    return out


def delete_knowledge_file(settings: Settings, rel_path: str) -> Path:
    """Delete a file inside knowledge_dir (path-safe). Chunks are removed on the next ingest."""
    base = settings.knowledge_dir.resolve()
    target = (base / rel_path).resolve()
    if base not in target.parents or not target.is_file():
        raise FileNotFoundError(f"not a knowledge file: {rel_path}")
    target.unlink()
    for cand in (target.with_suffix(".md"), target.with_suffix(".pdf")):
        pass
    return target
