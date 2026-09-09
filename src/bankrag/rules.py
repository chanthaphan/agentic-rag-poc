"""Responsible Lending rule packs: rules/<pack>/PACK.md plus one <rule-id>.md per rule.

A rule keeps the regulator's own text (กฎหมาย) and the compliance team's instruction (กฎสำหรับระบบ) verbatim in the
markdown body, and carries in its frontmatter the parts the app needs: which product families it covers, when it
applies to an answer, and how it is checked. The same rule reaches the agents twice:

  prompt time  `prompt_block_for_skill()` / `prompt_block_for_concierge()` are appended to the agent instructions, so
               the concierge knows which product families are regulated and every specialist carries its own rules.
               The block is part of the agent definition, so `bankrag skills sync` re-versions agents when rules change.
  answer time  `guard()` checks the drafted answer: mandated warnings that are missing are appended verbatim
               (enforcement: append), everything else is reported as a finding on the turn's trace.

Verdicts follow the sheet's vocabulary: compliant / non_compliant / undefined (a human or the LLM judge has to look)
/ not_applicable.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

import frontmatter

from .config import Settings
from .models import RuleFinding, RulePack, RuleProduct, RuleSpec, SkillSpec

PACK_FILE = "PACK.md"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
STATUSES = ("active", "draft", "retired")
CHECKS = ("required_phrase", "prohibited_phrase", "required_pattern", "judgement")
ENFORCEMENTS = ("append", "flag", "none")
SEVERITIES = ("block", "warn")
SECTION_RE = re.compile(r"^#{1,6}[ \t]*(?P<head>[^\n]*)$", re.M)
SECTION_KEYS = (("assistant_note", ("หมายเหตุ", "assistant note", "note")), ("legal_text", ("กฎหมาย", "legal")), ("system_rule", ("ระบบ", "system")))
_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'", "«": '"', "»": '"'}


def rules_dir(settings: Settings) -> Path:
    return settings.rules_dir


# ---------------- text matching ----------------
def normalize(text: str) -> str:
    """Compare wording the way a reviewer would: NFC, smart quotes folded, whitespace collapsed, case-insensitive."""
    text = unicodedata.normalize("NFC", text or "")
    for a, b in _QUOTES.items():
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def _contains(haystack_norm: str, phrase: str) -> bool:
    p = normalize(phrase)
    return bool(p) and p in haystack_norm


def _any_pattern(text: str, patterns: Iterable[str]) -> Optional[str]:
    for p in patterns:
        try:
            m = re.search(p, text, re.I | re.S)
        except re.error:
            continue
        if m:
            return m.group(0)[:120]
    return None


# ---------------- load ----------------
def _split_body(body: str) -> dict[str, str]:
    """The markdown body as its three sections: กฎหมาย (verbatim law), กฎสำหรับระบบ (verbatim instruction from the
    sheet) and หมายเหตุ (our note on how it applies in chat). An unstructured body counts as the system rule."""
    heads = list(SECTION_RE.finditer(body))
    if not heads:
        return {"legal_text": "", "system_rule": body.strip(), "assistant_note": ""}
    out = {"legal_text": "", "system_rule": "", "assistant_note": ""}
    for i, m in enumerate(heads):
        head = m.group("head").lower()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        key = next((k for k, words in SECTION_KEYS if any(w in head for w in words)), "")
        if key and not out[key]:
            out[key] = body[m.end():end].strip()
    if not out["system_rule"] and not out["legal_text"]:
        out["system_rule"] = body.strip()
    return out


def parse_rule(path: Path, pack_id: str = "") -> RuleSpec:
    post = frontmatter.load(path)
    meta = dict(post.metadata)
    sections = _split_body(post.content.strip())
    listy = lambda k: [str(x).strip() for x in (meta.get(k) or []) if str(x).strip()]  # noqa: E731
    return RuleSpec(
        id=str(meta.get("id") or path.stem),
        pack=str(meta.get("pack") or pack_id),
        title=str(meta.get("title") or path.stem),
        regulation=str(meta.get("regulation") or ""),
        clause=str(meta.get("clause") or "").strip(),
        products=listy("products"),
        status=str(meta.get("status") or "active").strip().lower(),
        severity=str(meta.get("severity") or "block").strip().lower(),
        check=str(meta.get("check") or "judgement").strip().lower(),
        enforcement=str(meta.get("enforcement") or "flag").strip().lower(),
        phrases=listy("phrases"),
        patterns=listy("patterns"),
        applies_when=listy("applies_when"),
        disclosure={str(k): str(v) for k, v in (meta.get("disclosure") or {}).items()},
        template=str(meta.get("template") or ""),
        legal_text=sections["legal_text"],
        system_rule=sections["system_rule"],
        assistant_note=sections["assistant_note"],
        path=path,
    )


def load_pack(settings: Settings, pack_id: str = "mccs") -> RulePack:
    """Read rules/<pack>/: PACK.md (products + pack metadata) and every other .md file as a rule."""
    d = rules_dir(settings) / pack_id
    if not (d / PACK_FILE).exists():
        return RulePack(id=pack_id, path=d)
    post = frontmatter.load(d / PACK_FILE)
    meta = dict(post.metadata)
    products = [RuleProduct(**{**{"id": "", "name": ""}, **p}) for p in (meta.get("products") or [])]
    rules = [parse_rule(f, pack_id) for f in sorted(d.glob("*.md")) if f.name != PACK_FILE]
    return RulePack(id=str(meta.get("id") or pack_id), name=str(meta.get("name") or pack_id), description=str(meta.get("description") or ""),
                    sources=[str(x) for x in (meta.get("sources") or [])], products=products, body=post.content.strip(),
                    rules=sorted(rules, key=lambda r: (r.clause, r.id)), path=d)


_cache: dict[str, tuple[float, RulePack]] = {}


def active_pack(settings: Settings, pack_id: str = "mccs") -> RulePack:
    """load_pack() cached on the folder's newest mtime, so Studio edits apply without a restart."""
    d = rules_dir(settings) / pack_id
    stamp = max((f.stat().st_mtime for f in d.glob("*.md")), default=0.0) if d.exists() else 0.0
    hit = _cache.get(pack_id)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    pack = load_pack(settings, pack_id)
    _cache[pack_id] = (stamp, pack)
    return pack


def list_packs(settings: Settings) -> list[str]:
    d = rules_dir(settings)
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / PACK_FILE).exists()) if d.exists() else []


# ---------------- validate ----------------
def validate_rule(rule: RuleSpec, pack: RulePack) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not ID_RE.match(rule.id):
        errors.append(f"id '{rule.id}' must match {ID_RE.pattern}")
    if rule.status not in STATUSES:
        errors.append(f"status '{rule.status}' must be one of {', '.join(STATUSES)}")
    if rule.check not in CHECKS:
        errors.append(f"check '{rule.check}' must be one of {', '.join(CHECKS)}")
    if rule.enforcement not in ENFORCEMENTS:
        errors.append(f"enforcement '{rule.enforcement}' must be one of {', '.join(ENFORCEMENTS)}")
    if rule.severity not in SEVERITIES:
        errors.append(f"severity '{rule.severity}' must be one of {', '.join(SEVERITIES)}")
    if not rule.products:
        errors.append("products is empty: the rule would never apply to an answer")
    for pid in rule.products:
        if pack.product(pid) is None:
            errors.append(f"unknown product '{pid}' (add it to {PACK_FILE})")
    if not rule.system_rule:
        errors.append("the '## กฎสำหรับระบบ' section is empty: the agent would get no instruction")
    if rule.check in ("required_phrase", "prohibited_phrase") and not rule.phrases and not rule.patterns:
        errors.append(f"check '{rule.check}' needs phrases (or patterns)")
    if rule.check == "required_pattern" and not rule.patterns:
        errors.append("check 'required_pattern' needs patterns")
    if rule.enforcement == "append" and not rule.disclosure:
        errors.append("enforcement 'append' needs a disclosure (th / en) to append")
    for p in rule.patterns + rule.applies_when:
        try:
            re.compile(p)
        except re.error as e:
            errors.append(f"bad regex {p!r}: {e}")
    if not rule.clause:
        warnings.append("no clause reference (ข้อกฎหมาย); reviewers cannot trace the rule back")
    if rule.check == "judgement" and rule.severity == "block":
        warnings.append("judgement rules cannot be decided at answer time; every applicable answer is reported as 'undefined'")
    return errors, warnings


def validate_pack(pack: RulePack) -> dict[str, tuple[list[str], list[str]]]:
    out = {r.id: validate_rule(r, pack) for r in pack.rules}
    dupes = [p.id for p in pack.products if sum(1 for q in pack.products if q.id == p.id) > 1]
    if dupes:
        out.setdefault("(pack)", ([], []))[0].append(f"duplicate product ids: {', '.join(sorted(set(dupes)))}")
    return out


# ---------------- product detection ----------------
def products_for_skill(pack: RulePack, skill_id: str) -> list[str]:
    return [p.id for p in pack.products if skill_id in p.skills]


def detect_products(pack: RulePack, text: str, *, skill_id: str = "") -> list[str]:
    """Product families a piece of text is about. The routed skill only counts as a hint for its own families."""
    norm = normalize(text)
    hinted = set(products_for_skill(pack, skill_id)) if skill_id else set()
    found: list[str] = []
    for p in pack.products:
        terms = [p.name, *p.aliases]
        if any(_contains(norm, t) for t in terms) or _any_pattern(text, p.match):
            found.append(p.id)
    if not found and len(hinted) == 1:  # a specialist agent whose whole product family is regulated
        found = sorted(hinted)
    return found


def rules_for_products(pack: RulePack, product_ids: Iterable[str], *, status: str = "active") -> list[RuleSpec]:
    wanted = set(product_ids)
    return [r for r in pack.rules if (not status or r.status == status) and wanted & set(r.products)]


# ---------------- prompt blocks ----------------
HEADER = "# Responsible Lending (ปฏิบัติตามก่อนตอบ / mandatory before you answer)"
INTRO = (
    "These rules come from {sources}. They apply to what YOU write, not only to marketing material: an answer that "
    "mentions or recommends a regulated product below is an advertisement under these rules. Follow every rule that "
    "matches the product you are talking about. Required wording must be reproduced EXACTLY, with no edit, translation "
    "or paraphrase, on its own line at the end of the answer."
)
FOOTER = (
    "## How to comply\n"
    "- Put the required warning(s) verbatim on their own line at the end of the answer, after the product facts.\n"
    "- If you cannot state every figure a rule requires (reference rate and its date, effective rate range, calculation "
    "assumptions), do NOT quote an interest rate, a fee waiver or an instalment amount at all: describe the product "
    "qualitatively and tell the customer where the exact figures are confirmed.\n"
    "- Never use wording that encourages borrowing beyond need, promises approval without a credit check, or makes "
    "borrowing sound effortless, in any language.\n"
    "- These warnings are required even when the customer did not ask about interest, and even in a short answer."
)


def _rule_lines(rule: RuleSpec, pack: RulePack, scope: Optional[set[str]] = None) -> str:
    pids = [p for p in rule.products if scope is None or p in scope]
    names = ", ".join((pack.product(p).name if pack.product(p) else p) for p in pids)
    out = [f"### {rule.clause or rule.id} — {rule.title}", f"Applies to: {names}", rule.system_rule.strip(), rule.assistant_note.strip()]
    if rule.template:
        out.append(f"Required wording / format: {rule.template.strip()}")
    if rule.check == "required_phrase" and rule.phrases:
        out.append("Required text (verbatim, one of): " + " | ".join(f'"{p}"' for p in rule.phrases))
    if rule.check == "prohibited_phrase" and rule.phrases:
        out.append("Never write (examples): " + ", ".join(f'"{p}"' for p in rule.phrases[:12]))
    return "\n".join(x for x in out if x)


def _block(pack: RulePack, rules: list[RuleSpec], scope_line: str, scope: Optional[set[str]] = None) -> str:
    if not rules:
        return ""
    parts = [HEADER, INTRO.format(sources="; ".join(pack.sources) or pack.name), scope_line, ""]
    parts += [_rule_lines(r, pack, scope) + "\n" for r in rules]
    parts.append(FOOTER)
    return "\n".join(parts).strip()


def prompt_block_for_skill(pack: RulePack, spec: SkillSpec) -> str:
    """The rules a specialist agent must carry: those covering the product families its skill answers about."""
    pids = products_for_skill(pack, spec.id)
    rules = rules_for_products(pack, pids)
    if not rules:
        return ""
    names = ", ".join(p.name for p in pack.products if p.id in set(pids))
    return _block(pack, rules, f"Regulated products in your scope: {names}.", set(pids))


def prompt_block_for_concierge(pack: RulePack) -> str:
    """What the concierge must know before it hands a question over and before it relays an answer."""
    rules = [r for r in pack.rules if r.status == "active"]
    if not rules:
        return ""
    lines = []
    for p in pack.products:
        rs = [r for r in rules if p.id in r.products]
        if rs:
            clauses = sorted({r.clause or r.id for r in rs})
            lines.append(f"- {p.name}: {len(rs)} rule(s) — {', '.join(clauses)}")
    required = [f'  - {", ".join(pack.product(p).name if pack.product(p) else p for p in r.products)}: "{r.phrases[0]}"'
                for r in rules if r.check == "required_phrase" and r.phrases]
    return "\n".join([
        HEADER,
        INTRO.format(sources="; ".join(pack.sources) or pack.name),
        "",
        "## Regulated product families (a question about any of them is a Responsible Lending answer)",
        *lines,
        "",
        "## Before you relay a specialist's answer",
        "- Check that the answer carries the warning its product family requires. If it is missing, add it verbatim on its"
        " own line at the end before you send the answer; never reword or translate it.",
        *(["- Required warnings:", *required] if required else []),
        "- If the answer quotes an interest rate, a fee waiver or an instalment amount without the assumptions and the"
        " reference-rate date the rules require, drop the figure from the answer and point the customer to the bank for"
        " the exact terms rather than sending an incomplete disclosure.",
        "- Never relay wording that encourages borrowing beyond need, promises approval without a credit check, or makes"
        " borrowing sound effortless.",
    ]).strip()


# ---------------- answer-time guard ----------------
def evaluate(pack: RulePack, text: str, *, question: str = "", skill_id: str = "") -> tuple[list[RuleFinding], list[str]]:
    """Check a drafted answer against the pack. Returns (findings, detected product ids)."""
    haystack = f"{question}\n{text}"
    pids = detect_products(pack, haystack, skill_id=skill_id)
    findings: list[RuleFinding] = []
    if not pids:
        return findings, pids
    norm = normalize(text)
    for rule in rules_for_products(pack, pids):
        f = RuleFinding(rule_id=rule.id, clause=rule.clause, title=rule.title, severity=rule.severity,
                        products=sorted(set(rule.products) & set(pids)))
        if rule.applies_when and _any_pattern(text, rule.applies_when) is None:
            f.verdict, f.detail = "not_applicable", "the answer does not contain the information this rule governs"
        elif rule.check == "required_phrase":
            if any(_contains(norm, p) for p in rule.phrases) or _any_pattern(text, rule.patterns):
                f.verdict = "compliant"
            else:
                f.verdict, f.detail = "non_compliant", f"missing required wording: {rule.phrases[0] if rule.phrases else rule.template}"
        elif rule.check == "prohibited_phrase":
            hit = next((p for p in rule.phrases if _contains(norm, p)), None) or _any_pattern(text, rule.patterns)
            f.verdict, f.detail = ("non_compliant", f"prohibited wording: {hit}") if hit else ("compliant", "")
        elif rule.check == "required_pattern":
            hit = _any_pattern(text, rule.patterns)
            f.verdict, f.detail = ("compliant", "") if hit else ("non_compliant", f"the answer must state: {rule.template or rule.title}")
        else:  # judgement: only a reviewer or the LLM judge can decide
            f.verdict, f.detail = "undefined", "judgement rule: enforced in the agent instructions, reviewed by the compliance eval"
        findings.append(f)
    return findings, pids


def disclosure_text(rule: RuleSpec, language: str) -> str:
    return rule.disclosure.get(language) or rule.disclosure.get("th") or (rule.phrases[0] if rule.phrases else "")


def apply_disclosures(text: str, rules: list[RuleSpec], language: str) -> tuple[str, list[str]]:
    """Append the mandated warnings that are missing, verbatim, as a block at the end of the answer."""
    added = [d for d in (disclosure_text(r, language) for r in rules) if d and not _contains(normalize(text), d)]
    if not added:
        return text, []
    block = "\n\n---\n" + "\n".join(f"⚠️ {a}" for a in added)
    return text.rstrip() + block, added


def guard(settings: Settings, text: str, *, question: str = "", language: str = "th", skill_id: str = "",
          pack_id: str = "mccs") -> tuple[str, dict]:
    """Answer-time enforcement. Returns (answer text, report) where report goes on the turn's trace.

    `appended` is the text added to the answer, so a streaming caller can emit it as one more delta."""
    pack = active_pack(settings, pack_id)
    if not pack.rules:
        return text, {}
    findings, pids = evaluate(pack, text, question=question, skill_id=skill_id)
    if not findings:
        return text, {"pack": pack.id, "products": pids, "checked": 0, "findings": []}
    by_id = {r.id: r for r in pack.rules}
    to_append = [by_id[f.rule_id] for f in findings
                 if f.verdict == "non_compliant" and by_id[f.rule_id].enforcement == "append"]
    fixed_text, added = apply_disclosures(text, to_append, language)
    fixed_ids = {r.id for r in to_append}
    for f in findings:
        if f.rule_id in fixed_ids and added:
            f.fixed, f.verdict, f.detail = True, "compliant", f"{f.detail}; the required wording was added to the answer"
    report = {
        "pack": pack.id,
        "products": pids,
        "product_names": [pack.product(p).name for p in pids if pack.product(p)],
        "checked": len(findings),
        "fixed": sorted(fixed_ids) if added else [],
        "violations": [f.rule_id for f in findings if f.verdict == "non_compliant"],
        "review": [f.rule_id for f in findings if f.verdict == "undefined"],
        "findings": [f.model_dump() for f in findings],
    }
    return fixed_text, {**report, "appended": fixed_text[len(text):] if added else ""}


# ---------------- editing (Studio / CLI) ----------------
FRONT_KEYS = ("id", "pack", "title", "regulation", "clause", "products", "status", "severity", "check", "enforcement",
              "phrases", "patterns", "applies_when", "disclosure", "template")


def rule_path(settings: Settings, pack_id: str, rule_id: str) -> Path:
    if not ID_RE.match(rule_id) or not ID_RE.match(pack_id):
        raise ValueError(f"invalid rule id '{rule_id}'")
    return rules_dir(settings) / pack_id / f"{rule_id}.md"


def dump_rule(rule: RuleSpec) -> str:
    meta = {k: getattr(rule, k) for k in FRONT_KEYS}
    meta = {k: v for k, v in meta.items() if v not in ("", [], {}, None)}
    body = f"## กฎหมาย (legal text)\n{rule.legal_text}\n\n## กฎสำหรับระบบ (system rule)\n{rule.system_rule}\n"
    if rule.assistant_note:
        body += f"\n## หมายเหตุสำหรับผู้ช่วย (assistant note)\n{rule.assistant_note}\n"
    return frontmatter.dumps(frontmatter.Post(body, **meta), sort_keys=False, allow_unicode=True, width=100000) + "\n"


def write_rule(settings: Settings, pack_id: str, rule: RuleSpec) -> RuleSpec:
    p = rule_path(settings, pack_id, rule.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_rule(rule), encoding="utf-8")
    _cache.pop(pack_id, None)
    return parse_rule(p, pack_id)


def update_rule(settings: Settings, pack_id: str, rule_id: str, form: dict) -> RuleSpec:
    """Patch one rule from a Studio form (only the keys present are changed)."""
    p = rule_path(settings, pack_id, rule_id)
    if not p.exists():
        raise FileNotFoundError(f"rule '{rule_id}' does not exist in pack '{pack_id}'")
    rule = parse_rule(p, pack_id)
    data = rule.model_dump()
    for k, v in form.items():
        if k in FRONT_KEYS and k != "id":
            data[k] = v
    for k in ("legal_text", "system_rule", "assistant_note"):
        if k in form:
            data[k] = str(form[k])
    updated = RuleSpec(**{**data, "id": rule_id, "pack": pack_id})
    errors, _ = validate_rule(updated, active_pack(settings, pack_id))
    if errors:
        raise ValueError("; ".join(errors))
    return write_rule(settings, pack_id, updated)


def delete_rule(settings: Settings, pack_id: str, rule_id: str) -> None:
    p = rule_path(settings, pack_id, rule_id)
    if not p.exists():
        raise FileNotFoundError(f"rule '{rule_id}' does not exist in pack '{pack_id}'")
    p.unlink()
    _cache.pop(pack_id, None)
