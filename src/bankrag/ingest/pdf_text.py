"""Local PDF text extraction, tuned for Thai documents.

PyMuPDF reads the embedded BBLSans font ToUnicode maps correctly where the
remote parser mangled them. Image-only PDFs (no text layer) are detected and,
if OCR is available, run through Tesseract (Thai+English).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import fitz  # PyMuPDF

# Thai trailing vowels/tone marks (U+0E30..U+0E3A and U+0E47..U+0E4E). A space
# before any of these is an extraction artifact and is removed so a consonant
# and its following vowel/mark rejoin into one word. Built from code points so
# this source stays pure ASCII.
_THAI_TRAILING = "".join(
    chr(c) for c in list(range(0x0E30, 0x0E3B)) + list(range(0x0E47, 0x0E4F))
)
_SPACE_BEFORE_MARK = re.compile("[ \t]+([" + re.escape(_THAI_TRAILING) + "])")
_MULTISPACE = re.compile(r"[ \t]{2,}")
_MULTINEWLINE = re.compile(r"\n{3,}")
_ZERO_WIDTH = chr(0x200B)

# Average chars/page below this => treat as image-based (needs OCR).
_OCR_CHAR_THRESHOLD = 80


@dataclass
class ExtractResult:
    text: str
    pages: int
    chars: int
    image_based: bool
    method: str  # "text" | "ocr"


def normalize_thai(text: str) -> str:
    text = text.replace(_ZERO_WIDTH, "")
    text = _SPACE_BEFORE_MARK.sub(r"\1", text)
    text = _MULTISPACE.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _MULTINEWLINE.sub("\n\n", text)
    return text.strip()


def _text_layer(doc) -> str:
    return "\n".join(page.get_text("text") for page in doc)


def extract(pdf_path: str, *, ocr: bool = False, ocr_dpi: int = 200) -> ExtractResult:
    """Extract text from a PDF. Falls back to OCR for image-only pages when
    ``ocr`` is True and Tesseract is installed."""
    doc = fitz.open(pdf_path)
    pages = doc.page_count
    text = normalize_thai(_text_layer(doc))
    per_page = len(text) / max(pages, 1)
    image_based = per_page < _OCR_CHAR_THRESHOLD

    if image_based and ocr and ocr_available():
        ocr_text = normalize_thai(_ocr(doc, dpi=ocr_dpi))
        if len(ocr_text) > len(text):
            return ExtractResult(ocr_text, pages, len(ocr_text), True, "ocr")

    return ExtractResult(text, pages, len(text), image_based, "text")


# ---- OCR (optional) -------------------------------------------------
_OCR_CHECKED: Optional[bool] = None


def ocr_available() -> bool:
    global _OCR_CHECKED
    if _OCR_CHECKED is None:
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            _OCR_CHECKED = True
        except Exception:
            _OCR_CHECKED = False
    return _OCR_CHECKED


def _ocr(doc, *, dpi: int = 200, lang: str = "tha+eng") -> str:
    import io
    import pytesseract
    from PIL import Image

    out = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        out.append(pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))), lang=lang))
    return "\n".join(out)
