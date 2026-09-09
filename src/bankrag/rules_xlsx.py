"""The compliance team's spreadsheet <-> the rule files in rules/<pack>/.

The sheet (mccs-rules.xlsx) carries six columns: เล่มกฎหมาย, ข้อกฎหมาย, กฎหมาย, กฎสำหรับระบบ, ผลิตภัณฑ์ที่ต้องตรวจสอบ, สถานะ.
Import writes one file per row and keeps the engineering frontmatter (check, enforcement, phrases, patterns,
applies_when, disclosure, template, severity, trigger) of rules that already exist, so re-importing a refreshed sheet updates
the legal wording without throwing away how the rule is checked. Export writes the same columns back, plus the rule id.
"""
from __future__ import annotations

import hashlib
import io
import re
from typing import Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .config import Settings
from .models import RulePack, RuleProduct, RuleSpec
from .rules import PACK_FILE, active_pack, load_pack, normalize, pack_dir, write_rule

COLUMNS = {  # sheet header -> rule field
    "เล่มกฎหมาย": "regulation",
    "ข้อกฎหมาย": "clause",
    "กฎหมาย": "legal_text",
    "กฎสำหรับระบบ": "system_rule",
    "ผลิตภัณฑ์ที่ต้องตรวจสอบ": "products",
    "สถานะ": "status",
    "rule id": "id",
    "หัวข้อ": "title",
}
HEAD_FILL = PatternFill("solid", fgColor="0064FF")
HEAD_FONT = Font(bold=True, color="FFFFFF")


def _clean(v) -> str:
    """Cell text as one line, with CSV-escaped quote doubling undone (a single quote is part of the wording)."""
    return re.sub(r'"{2,}', '"', re.sub(r"\s+", " ", str(v or "")).strip()).strip()  # CSV-escaped quotes: """"…"""" -> "…"


def _clause_id(clause: str, taken: set[str]) -> str:
    nums = ".".join(re.findall(r"\d+", clause)) or hashlib.sha1(clause.encode()).hexdigest()[:6]
    base = "rule-" + nums.replace(".", "-")
    rid, n = base, 2
    while rid in taken:
        rid, n = f"{base}-{n}", n + 1
    return rid


def _match_product(pack: RulePack, name: str) -> tuple[Optional[str], str]:
    """(product id, note). Exact on the name or an alias, else a name-similarity match - but only when exactly one
    family is similar, so a broad sheet name like 'สินเชื่อ' is never bound silently to whichever family comes first."""
    n = normalize(name)
    for p in pack.products:
        if n == normalize(p.name) or any(n == normalize(a) for a in p.aliases):
            return p.id, ""
    similar = [p for p in pack.products if n and (n in normalize(p.name) or normalize(p.name) in n)]
    if len(similar) == 1:
        return similar[0].id, f"product '{name.strip()}' matched '{similar[0].name}' by name similarity, not exactly: check it is the family you mean"
    if len(similar) > 1:
        return None, f"product '{name.strip()}' looks like {len(similar)} families ({', '.join(p.id for p in similar)}); it was added as a new family instead of guessing"
    return None, ""


def _match_rule(pack: RulePack, row: dict) -> Optional[RuleSpec]:
    """The existing rule this row updates: the same id, else the same clause AND the same opening of the legal text.

    A clause on its own is not an identity - this pack already has three rules under 'ข้อ 2.2.1 (1)' - so a new row
    that reuses a clause with different wording becomes a new rule instead of overwriting an unrelated one."""
    if row.get("id"):
        hit = next((r for r in pack.rules if r.id == row["id"]), None)
        if hit is not None:
            return hit
    clause, legal = normalize(row.get("clause", "")), normalize(row.get("legal_text", ""))
    same_clause = [r for r in pack.rules if normalize(r.clause) == clause]
    exact = [r for r in same_clause if normalize(r.legal_text)[:60] == legal[:60]]
    if exact:
        return exact[0]
    if len(same_clause) == 1 and (not legal or not normalize(same_clause[0].legal_text)):
        return same_clause[0]  # one side has no legal text to compare, so the clause is all we have
    return None


def read_rows(data: bytes) -> list[dict]:
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [_clean(h) for h in rows[0]]
    fields = [COLUMNS.get(h, "") for h in header]
    if "system_rule" not in fields and "legal_text" not in fields:
        raise ValueError(f"unexpected sheet: no 'กฎสำหรับระบบ' or 'กฎหมาย' column (found: {', '.join(h for h in header if h)})")
    out = []
    for raw in rows[1:]:
        row = {f: _clean(v) for f, v in zip(fields, raw) if f}
        if any(row.get(k) for k in ("legal_text", "system_rule")):
            out.append(row)
    return out


def import_xlsx(settings: Settings, data: bytes, pack_id: str = "mccs", *, dry_run: bool = False) -> dict:
    """Write / update the rule files from a sheet. Returns what changed and what a reviewer has to look at."""
    pack = load_pack(settings, pack_id)
    if pack.path is None or not (pack.path / PACK_FILE).exists():
        raise ValueError(f"rules/{pack_id}/{PACK_FILE} does not exist: create the pack (product taxonomy) first")
    rows = read_rows(data)
    taken = {r.id for r in pack.rules}
    new_products: list[RuleProduct] = []
    result = {"pack": pack_id, "rows": len(rows), "created": [], "updated": [], "unchanged": [], "new_products": [], "warnings": []}
    pending: list[RuleSpec] = []
    for row in rows:
        existing = _match_rule(pack, row)
        pids: list[str] = []
        for name in [n for n in re.split(r"[,;]", row.get("products", "")) if n.strip()]:
            pid, note = _match_product(pack, name)
            if note:
                result["warnings"].append(note)
            if pid is None:
                pid = f"p-{hashlib.sha1(normalize(name).encode()).hexdigest()[:6]}"
                if not any(p.id == pid for p in pack.products + new_products):
                    new_products.append(RuleProduct(id=pid, name=name.strip(), match=[re.escape(name.strip())]))
                    result["warnings"].append(f"unknown product '{name.strip()}' added to {PACK_FILE} as '{pid}': give it a readable id, aliases and the skills that answer about it")
            if pid not in pids:
                pids.append(pid)
        if not pids:
            result["warnings"].append(f"row '{row.get('clause', '')}' has no product that maps to the pack; the rule would never apply")
        fields = {
            "id": existing.id if existing else (row.get("id") or _clause_id(row.get("clause", ""), taken)),
            "pack": pack_id,
            "title": row.get("title") or (existing.title if existing else _clean(row.get("system_rule", ""))[:70]),
            "regulation": row.get("regulation", ""),
            "clause": row.get("clause", ""),
            "products": sorted(pids),
            "status": (row.get("status") or "active").lower(),
            "legal_text": row.get("legal_text", ""),
            "system_rule": row.get("system_rule", ""),
        }
        if existing is None and row.get("clause") and any(normalize(r.clause) == normalize(row["clause"]) for r in pack.rules):
            result["warnings"].append(
                f"clause '{row['clause']}' already has a rule with different wording; this row was imported as a NEW rule "
                f"'{fields['id']}' rather than overwriting the existing one - merge or retire one of them in Studio")
        if existing is not None:  # keep how the rule is checked; the sheet only owns the legal columns
            keep = existing.model_dump(include={"trigger", "severity", "check", "enforcement", "phrases", "patterns", "applies_when", "disclosure", "template", "assistant_note", "extra_body"})
            rule = RuleSpec(**{**keep, **fields})
            bucket = "unchanged" if rule.model_dump(exclude={"path"}) == existing.model_dump(exclude={"path"}) else "updated"
        else:
            rule = RuleSpec(**fields, severity="warn", check="judgement", enforcement="flag")
            bucket = "created"
        taken.add(rule.id)
        result[bucket].append(rule.id)
        if bucket != "unchanged":
            pending.append(rule)
    if not dry_run:
        # families first: a rule must never be on disk referencing a product id PACK.md does not have yet
        if new_products:
            _append_products(settings, pack_id, new_products)
        for rule in pending:
            write_rule(settings, pack_id, rule)
    result["new_products"] = [p.id for p in new_products]
    return result


def _append_products(settings: Settings, pack_id: str, products: list[RuleProduct]) -> None:
    """Add product families the sheet mentioned but the pack did not know, so nothing is silently dropped."""
    import frontmatter

    p = pack_dir(settings, pack_id) / PACK_FILE
    post = frontmatter.load(p)
    post.metadata["products"] = list(post.metadata.get("products") or []) + [
        {"id": x.id, "name": x.name, "aliases": [], "skills": [], "match": x.match} for x in products]
    p.write_text(frontmatter.dumps(post, sort_keys=False, allow_unicode=True, width=100000) + "\n", encoding="utf-8")


def export_xlsx(settings: Settings, pack_id: str = "mccs") -> bytes:
    pack = active_pack(settings, pack_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "Export"
    headers = ["เล่มกฎหมาย", "ข้อกฎหมาย", "กฎหมาย", "กฎสำหรับระบบ", "ผลิตภัณฑ์ที่ต้องตรวจสอบ", "สถานะ", "rule id", "หัวข้อ", "การตรวจสอบ"]
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font, c.alignment = HEAD_FILL, HEAD_FONT, Alignment(vertical="center")
    for r in pack.rules:
        names = ", ".join((pack.product(p).name if pack.product(p) else p) for p in r.products)
        ws.append([r.regulation, r.clause, r.legal_text, r.system_rule, names, r.status, r.id, r.title,
                   f"{r.check} / {r.enforcement} / {r.severity}"])
    for col, width in zip("ABCDEFGHI", (34, 22, 70, 70, 34, 10, 28, 34, 26)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
