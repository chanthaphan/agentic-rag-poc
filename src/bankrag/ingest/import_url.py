"""Import individual web pages / PDFs by URL into knowledge/<category>/_imports/ (built-in fetch, no external service)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import frontmatter

from ..config import Settings
from . import clean as C
from .progress import report as progress

Log = Callable[[str], None]


def _slug(url: str) -> str:
    path = urlparse(url).path.rstrip("/").split("/")[-1] or urlparse(url).netloc
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-").lower()[:80] or "page"


def _fetch_bytes(url: str) -> tuple[bytes, str]:
    try:
        from curl_cffi import requests as creq  # type: ignore

        r = creq.get(url, impersonate="chrome", timeout=60)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "")
    except ImportError:
        import requests

        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "")


def _html_to_markdown(html: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    try:
        from markdownify import markdownify

        body = re.sub(r"<(script|style|nav|footer|noscript)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
        return markdownify(body, heading_style="ATX"), title
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"[ \t]+", " ", text), title


def import_urls(settings: Settings, category: str, urls: list[str], log: Log = print) -> list[str]:
    target = settings.knowledge_dir / category / "_imports"
    target.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    todo = [u.strip() for u in urls if u.strip()]
    for i, url in enumerate(todo):
        progress(log, "import", i, len(todo), message=url, imported=len(saved))
        if not url.startswith("http"):
            log(f"skip (not a URL): {url}")
            continue
        try:
            slug = _slug(url)
            if url.lower().endswith(".pdf"):
                data, _ = _fetch_bytes(url)
                pdf_path = target / f"{slug}.pdf"
                pdf_path.write_bytes(data)
                from .pdf_text import extract

                res = extract(str(pdf_path))
                text, title = res.text or "", slug
                if not text.strip():
                    log(f"warning: no text layer in {url}; kept the PDF only")
                    saved.append(str(pdf_path.relative_to(settings.knowledge_dir)))
                    continue
                dest = target / f"{slug}.md"
                doc_type = "pdf"
            else:
                data, _ = _fetch_bytes(url)
                text, title = _html_to_markdown(data.decode("utf-8", errors="replace"))
                dest = target / f"{slug}.md"
                doc_type = "product-page"
            text = C.clean_markdown(text, cut_footer=True) or text
            post = frontmatter.Post(text, title=title or slug, source_url=url, doc_type=doc_type)
            dest.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
            saved.append(str(dest.relative_to(settings.knowledge_dir)))
            log(f"imported {url} -> {dest.relative_to(settings.knowledge_dir)} ({len(text)} chars)")
        except Exception as e:  # noqa: BLE001
            log(f"ERROR {url}: {type(e).__name__}: {str(e)[:200]}")
    progress(log, "import", len(todo), len(todo), message="", imported=len(saved))
    return saved
