"""Heading-aware markdown chunking sized by o200k tokens; safe for Thai text (no word spaces)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import tiktoken

_ENC = tiktoken.get_encoding("o200k_base")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
# Thai combining marks (vowels above/below, tone marks) must never start a chunk.
_THAI_COMBINING = "".join(chr(c) for c in list(range(0x0E31, 0x0E3B)) + list(range(0x0E47, 0x0E4F)))


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text, disallowed_special=()))


@dataclass
class Section:
    breadcrumb: list[str]
    paragraphs: list[str] = field(default_factory=list)


def split_sections(text: str, title: str) -> list[Section]:
    """Split markdown into sections keyed by heading path (title > H2 > H3 ...)."""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current = Section(breadcrumb=[title])
    buf: list[str] = []

    def flush_para() -> None:
        if buf:
            para = "\n".join(buf).strip()
            if para:
                current.paragraphs.append(para)
            buf.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush_para()
            if current.paragraphs:
                sections.append(current)
            level, heading = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            current = Section(breadcrumb=[title] + [h for _, h in stack])
            continue
        if not line.strip():
            flush_para()
            continue
        buf.append(line.rstrip())
    flush_para()
    if current.paragraphs:
        sections.append(current)
    return sections


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Split an oversize paragraph on newline/space/Thai-safe boundaries so each piece fits."""
    if count_tokens(text) <= max_tokens:
        return [text]
    # approximate characters per token for this text, then cut with backoff to a safe boundary
    ratio = max(1.0, len(text) / max(1, count_tokens(text)))
    window = int(max_tokens * ratio * 0.9)
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + window)
        if end < len(text):
            cut = -1
            for sep in ("\n", " ", "。", ".", ",", ")"):
                pos = text.rfind(sep, start + window // 2, end)
                if pos > cut:
                    cut = pos
            if cut > start:
                end = cut + 1
            while end < len(text) and text[end] in _THAI_COMBINING:
                end += 1
        piece = text[start:end].strip()
        if piece:
            # guarantee the limit even if the ratio estimate was off
            while count_tokens(piece) > max_tokens and len(piece) > 20:
                piece = piece[: int(len(piece) * 0.8)].rstrip()
                while piece and piece[-1] in _THAI_COMBINING:
                    piece = piece[:-1]
                end = start + len(piece)
            pieces.append(piece)
        start = end
    return pieces


def chunk_markdown(text: str, title: str, *, target: int = 450, max_tokens: int = 700, overlap_paragraphs: int = 1) -> list[dict]:
    """Return chunks: [{"content", "breadcrumb", "chunk_index"}]. Content is prefixed with the breadcrumb."""
    chunks: list[dict] = []
    for section in split_sections(text, title):
        crumb = " > ".join(section.breadcrumb)
        prefix = f"{crumb}\n\n"
        prefix_tokens = count_tokens(prefix)
        budget = max(50, target - prefix_tokens)
        hard_max = max(budget, max_tokens - prefix_tokens)
        paras: list[str] = []
        for p in section.paragraphs:
            paras.extend(_hard_split(p, hard_max))
        current: list[str] = []
        current_tokens = 0
        for p in paras:
            pt = count_tokens(p) + 1
            if current and current_tokens + pt > budget:
                chunks.append({"content": prefix + "\n\n".join(current), "breadcrumb": crumb})
                keep = current[-overlap_paragraphs:] if overlap_paragraphs else []
                keep = [k for k in keep if count_tokens(k) < budget // 2]
                current = keep + [p]
                current_tokens = sum(count_tokens(k) + 1 for k in current)
            else:
                current.append(p)
                current_tokens += pt
        if current:
            chunks.append({"content": prefix + "\n\n".join(current), "breadcrumb": crumb})
    for i, c in enumerate(chunks):
        c["chunk_index"] = i
    return chunks
