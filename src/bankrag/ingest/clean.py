"""Clean crawled bangkokbank.com markdown (and generic markdown) before chunking."""
from __future__ import annotations

import re

_HTML_COMMENT = re.compile(r"<!--\s*([a-zA-Z_]+):\s*(.*?)\s*-->")
_IMAGE_ONLY = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_INLINE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HEADING_IMAGE = re.compile(r"^(#{1,6})\s*!\[[^\]]*\]\([^)]*\)\s*")
_LINK_MD = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_MULTI_BLANK = re.compile(r"\n{3,}")

# The crawled product pages end with site chrome: help tools, contact/consult section, cookie
# banner, leaving-site modal, PDPA notice. Cut at the first of these markers (heading form only).
_FOOTER_MARKERS = (
    re.compile(r"^#{1,6}\s*เครื่องมือช่วยเหลือ"),
    re.compile(r"^#{1,6}\s*ธนาคารพร้อมให้คำปรึกษา"),
    re.compile(r"^#{1,6}\s*.*คุกกี้"),
    re.compile(r"^#{1,6}\s*คุณกำลังจะออกจากเว็บไซต์"),
    re.compile(r"^#{1,6}\s*หนังสือแจ้งการคุ้มครองข้อมูลส่วนบุคคล"),
)
# Site navigation list that appears right under the hero (bullets naming site sections).
_NAV_BULLETS = {
    "บัตรเครดิตธนาคารกรุงเทพ", "โปรโมชันบัตรเครดิต", "โปรโมชันผู้สมัครบัตรใหม่", "บริการบัตรเครดิต",
    "ข้อมูลอื่นๆ", "คำถามที่พบบ่อย", "เครื่องมือช่วยเหลือ", "บริการ ดิจิทัล วอลเล็ท", "บริการบัวหลวง ไอเพย์",
    "บริการแบ่งชำระรายเดือนกับ Be Smart", "มาตรการบรรเทาบรรเทาภาระหนี้สินเชื่อบัตรเครดิต",
}


def extract_meta(text: str) -> dict[str, str]:
    """Read `<!-- key: value -->` comments (the crawler writes url/title)."""
    return {m.group(1).lower(): m.group(2).strip() for m in _HTML_COMMENT.finditer(text)}


def clean_markdown(text: str, *, cut_footer: bool = True) -> str:
    text = text.replace("\r\n", "\n").replace("​", "")
    text = _HTML_COMMENT.sub("", text)
    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if cut_footer and any(m.match(line) for m in _FOOTER_MARKERS):
            break
        if _IMAGE_ONLY.match(line):
            continue
        line = _HEADING_IMAGE.sub(r"\1 ", line)
        line = _INLINE_IMAGE.sub("", line)
        if line.strip() in ("ตกลง", "ยกเลิก", "ปิด"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].strip() in _NAV_BULLETS:
            continue
        if stripped.startswith("#") and stripped.lstrip("#").strip() == "":
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = _dedupe_blocks(text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip() + "\n" if text.strip() else ""


def _dedupe_blocks(text: str) -> str:
    """Drop exact-duplicate paragraphs/headings (the pages render hero text twice)."""
    seen: set[str] = set()
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        key = " ".join(block.split())
        if not key:
            continue
        if key in seen and len(key) > 12:
            continue
        seen.add(key)
        out.append(block.strip("\n"))
    return "\n\n".join(out)


def detect_language(text: str) -> str:
    thai = sum(1 for ch in text if "฀" <= ch <= "๿")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if thai == 0 and latin == 0:
        return "th"
    ratio = thai / max(1, thai + latin)
    if ratio > 0.7:
        return "th"
    if ratio < 0.2:
        return "en"
    return "mixed"


def first_heading(text: str) -> str:
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*\S)\s*$", line)
        if m:
            return _INLINE_IMAGE.sub("", m.group(1)).strip()
    return ""
