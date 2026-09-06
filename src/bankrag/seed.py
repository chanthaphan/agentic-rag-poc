"""Seed knowledge/credit-card from the bblwebsite_crawler output (data/products/th-TH/Personal/Cards)."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Callable

import frontmatter

from .ingest import clean as C

DEFAULT_CRAWLER_DATA = Path("/Users/atthawutchanthaphan/Codes/SmallWork/bblwebsite_crawler/data/products/th-TH/Personal/Cards")
CARD_SECTIONS = ("Credit-Cards", "BangkokBankM")
GENERAL_SLUGS = {"Rewards": "product-page", "GooglePay": "product-page", "TermsandConditions": "terms"}
Log = Callable[[str], None]


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return s or "item"


def _write_md(dest: Path, body: str, meta: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, **{k: v for k, v in meta.items() if v not in (None, "")})
    dest.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


def seed_credit_cards(crawler_data: Path, knowledge_dir: Path, *, include_promotions: bool = False, clear: bool = False, log: Log = print) -> dict[str, int]:
    target = knowledge_dir / "credit-card"
    if clear and target.exists():
        shutil.rmtree(target)
    counts = {"products": 0, "pages": 0, "pdfs": 0, "promotions": 0, "general": 0}
    seen_promo_urls: set[str] = set()
    for section in CARD_SECTIONS:
        sec_dir = crawler_data / section
        if not sec_dir.exists():
            log(f"missing section folder: {sec_dir}")
            continue
        for prod_dir in sorted(p for p in sec_dir.iterdir() if p.is_dir()):
            pj = prod_dir / "product.json"
            if not pj.exists():
                continue
            info = json.loads(pj.read_text(encoding="utf-8"))
            slug_raw = prod_dir.name
            title = info.get("title") or slug_raw
            url = info.get("url", "")
            if slug_raw == "Promotions":
                if not include_promotions:
                    continue
                for md in sorted(prod_dir.rglob("*.md")):
                    text = md.read_text(encoding="utf-8", errors="replace")
                    meta = C.extract_meta(text)
                    purl = meta.get("url", "")
                    if purl in seen_promo_urls:
                        continue
                    seen_promo_urls.add(purl)
                    dest = target / "_promotions" / f"{_slug(md.stem)}.md"
                    _write_md(dest, text, {"title": meta.get("title") or md.stem, "source_url": purl, "doc_type": "promotion"})
                    counts["promotions"] += 1
                continue
            is_general = slug_raw in GENERAL_SLUGS
            slug = _slug(slug_raw)
            prod_target = target / ("_general/" + slug if is_general else slug)
            page = prod_dir / "page.md"
            if page.exists():
                text = page.read_text(encoding="utf-8", errors="replace")
                _write_md(
                    prod_target / "page.md",
                    text,
                    {"title": title, "source_url": url, "product_name": "" if is_general else title, "doc_type": GENERAL_SLUGS.get(slug_raw, "product-page")},
                )
                counts["general" if is_general else "pages"] += 1
                if not is_general:
                    counts["products"] += 1
            for pdf_url, pdf in (info.get("pdfs") or {}).items():
                text_path = pdf.get("text_path")
                if not text_path:
                    continue
                src = prod_dir / text_path
                if not src.exists():
                    continue
                dest = prod_target / "pdfs" / (Path(pdf.get("filename", src.name)).stem + ".md")
                body = src.read_text(encoding="utf-8", errors="replace")
                pdf_title = f"{title} - {Path(pdf.get('filename', src.name)).stem}"
                _write_md(dest, body, {"title": pdf_title, "source_url": pdf_url, "product_name": "" if is_general else title, "doc_type": "pdf", "cut_footer": False})
                counts["pdfs"] += 1
    log(f"seeded into {target}: {counts}")
    return counts
