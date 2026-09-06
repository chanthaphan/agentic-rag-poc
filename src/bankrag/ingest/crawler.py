"""Built-in site crawler (no external service): fetch pages under a URL prefix, convert to markdown, download linked PDFs.

Output: knowledge/<category>/_crawl/<slug>.md (+ .pdf and its extracted .md). A per-category manifest
(.state/crawl/<category>.json) records url -> sha256 so re-crawls only rewrite changed pages.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import frontmatter

from ..config import Settings
from . import clean as C

Log = Callable[[str], None]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".ico", ".mp4", ".zip", ".xlsx", ".docx", ".pptx")


def fetch(url: str, timeout: int = 60) -> tuple[bytes, str, int]:
    """(body, content-type, status). Uses curl_cffi with Chrome impersonation when available (needed for Akamai-fronted sites)."""
    try:
        from curl_cffi import requests as creq  # type: ignore

        r = creq.get(url, impersonate="chrome", timeout=timeout, headers={"Accept-Language": "th,en;q=0.8"})
        return r.content, r.headers.get("content-type", ""), r.status_code
    except ImportError:
        import requests

        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA, "Accept-Language": "th,en;q=0.8"})
        return r.content, r.headers.get("content-type", ""), r.status_code


def slug_for(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p]
    tail = "-".join(parts[-2:]) if parts else urlparse(url).netloc
    return re.sub(r"[^A-Za-z0-9]+", "-", tail).strip("-").lower()[:90] or "page"


def _norm(url: str) -> str:
    url, _ = urldefrag(url)
    return url.rstrip("/") if urlparse(url).path not in ("", "/") else url


def in_scope(url: str, prefixes: Iterable[str]) -> bool:
    u = url.lower()
    return any(u.startswith(p.lower().rstrip("/")) for p in prefixes)


def html_to_markdown(html: str, base_url: str) -> tuple[str, str, list[str]]:
    """Returns (markdown of the main content, title, absolute links found in the page)."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        links.append(_norm(urljoin(base_url, href)))
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header", "form", "button"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    md = markdownify(str(main), heading_style="ATX", strip=["img"])
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip(), title, links


def _manifest_path(settings: Settings, category: str) -> Path:
    return settings.state_dir / "crawl" / f"{category}.json"


def crawl(
    settings: Settings,
    category: str,
    start_urls: list[str],
    *,
    include_prefixes: Optional[list[str]] = None,
    max_pages: int = 50,
    include_pdfs: bool = True,
    delay_s: float = 0.5,
    log: Log = print,
) -> dict:
    """BFS crawl from start_urls, restricted to include_prefixes (default: the start URLs' own paths)."""
    prefixes = [p.strip() for p in (include_prefixes or []) if p.strip()] or [_norm(u) for u in start_urls]
    target = settings.knowledge_dir / category / "_crawl"
    target.mkdir(parents=True, exist_ok=True)
    mp = _manifest_path(settings, category)
    manifest = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    queue = deque(_norm(u) for u in start_urls if u.strip())
    seen: set[str] = set(queue)
    stats = {"fetched": 0, "pages": 0, "pdfs": 0, "unchanged": 0, "errors": 0, "skipped_out_of_scope": 0, "files": []}
    pdf_queue: list[str] = []

    while queue and stats["pages"] + stats["unchanged"] < max_pages:
        url = queue.popleft()
        if url.lower().endswith(SKIP_EXT):
            continue
        if url.lower().endswith(".pdf"):
            pdf_queue.append(url)
            continue
        try:
            body, ctype, status = fetch(url)
            stats["fetched"] += 1
            if status >= 400:
                log(f"HTTP {status}: {url}")
                stats["errors"] += 1
                continue
            if "pdf" in ctype.lower():
                pdf_queue.append(url)
                continue
            html = body.decode("utf-8", errors="replace")
            md, title, links = html_to_markdown(html, url)
            for link in links:
                if link in seen:
                    continue
                if link.lower().endswith(".pdf"):
                    if include_pdfs and (in_scope(link, prefixes) or urlparse(link).netloc == urlparse(url).netloc):
                        seen.add(link)
                        pdf_queue.append(link)
                    continue
                if in_scope(link, prefixes):
                    seen.add(link)
                    queue.append(link)
                else:
                    stats["skipped_out_of_scope"] += 1
            cleaned = C.clean_markdown(md, cut_footer=True) or md
            digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
            slug = slug_for(url)
            dest = target / f"{slug}.md"
            if manifest.get(url, {}).get("sha256") == digest and dest.exists():
                stats["unchanged"] += 1
                log(f"unchanged: {url}")
            else:
                post = frontmatter.Post(cleaned, title=title or slug, source_url=url, doc_type="product-page", crawled_at=datetime.now(timezone.utc).isoformat())
                dest.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
                manifest[url] = {"sha256": digest, "file": str(dest.relative_to(settings.knowledge_dir)), "title": title}
                stats["pages"] += 1
                stats["files"].append(str(dest.relative_to(settings.knowledge_dir)))
                log(f"page: {title[:60] or slug} <- {url} ({len(cleaned)} chars)")
        except Exception as e:  # noqa: BLE001
            stats["errors"] += 1
            log(f"ERROR {url}: {type(e).__name__}: {str(e)[:160]}")
        time.sleep(delay_s)

    if include_pdfs:
        for url in pdf_queue[: max(0, max_pages * 2)]:
            try:
                body, _ctype, status = fetch(url)
                stats["fetched"] += 1
                if status >= 400 or len(body) < 1000:
                    log(f"pdf skip (HTTP {status}, {len(body)} bytes): {url}")
                    stats["errors"] += 1
                    continue
                digest = hashlib.sha256(body).hexdigest()
                slug = slug_for(url)
                pdf_path = target / f"{slug}.pdf"
                if manifest.get(url, {}).get("sha256") == digest and pdf_path.with_suffix(".md").exists():
                    stats["unchanged"] += 1
                    continue
                pdf_path.write_bytes(body)
                from .pdf_text import extract

                res = extract(str(pdf_path))
                text = res.text or ""
                if text.strip():
                    post = frontmatter.Post(text, title=slug, source_url=url, doc_type="pdf", cut_footer=False, crawled_at=datetime.now(timezone.utc).isoformat())
                    pdf_path.with_suffix(".md").write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
                    stats["files"].append(str(pdf_path.with_suffix(".md").relative_to(settings.knowledge_dir)))
                    log(f"pdf: {slug} ({len(text)} chars) <- {url}")
                else:
                    log(f"pdf without text layer (kept file, not ingestible): {url}")
                manifest[url] = {"sha256": digest, "file": str(pdf_path.relative_to(settings.knowledge_dir))}
                stats["pdfs"] += 1
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log(f"ERROR {url}: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(delay_s)

    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if queue:
        log(f"stopped at max_pages={max_pages}; {len(queue)} URLs left in the queue")
    log(f"crawl done: {stats['pages']} new/changed pages, {stats['unchanged']} unchanged, {stats['pdfs']} PDFs, {stats['errors']} errors")
    return stats
