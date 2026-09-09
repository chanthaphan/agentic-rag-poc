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
import time
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
TRIGGERS = ("promotion", "mention")
SEVERITIES = ("block", "warn")
SECTION_RE = re.compile(r"^#{1,6}[ \t]*(?P<head>[^\n]*)$", re.M)
# order matters: a heading is classified by the first entry it matches, so the two MCCS sections win over the note
SECTION_KEYS = (("legal_text", ("กฎหมาย", "legal")), ("system_rule", ("ระบบ", "system")), ("assistant_note", ("หมายเหตุ", "assistant note")))
_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'", "«": '"', "»": '"'}


def rules_dir(settings: Settings) -> Path:
    return settings.rules_dir


def pack_dir(settings: Settings, pack_id: str) -> Path:
    """The pack folder, with the id validated: it reaches us from a request body / query string."""
    if not ID_RE.match(pack_id or ""):
        raise ValueError(f"invalid pack id '{pack_id}'")
    return rules_dir(settings) / pack_id


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
    """The markdown body as its three known sections: กฎหมาย (verbatim law), กฎสำหรับระบบ (verbatim instruction from
    the sheet) and หมายเหตุ (our note on how it applies in chat). Anything else is kept verbatim in `extra_body` so a
    save from Studio cannot silently delete a section someone added. An unstructured body counts as the system rule."""
    heads = list(SECTION_RE.finditer(body))
    if not heads:
        return {"legal_text": "", "system_rule": body.strip(), "assistant_note": "", "extra_body": ""}
    out = {"legal_text": "", "system_rule": "", "assistant_note": "", "extra_body": ""}
    extra: list[str] = []
    for i, m in enumerate(heads):
        head = m.group("head").lower()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        key = next((k for k, words in SECTION_KEYS if any(w in head for w in words)), "")
        if key and not out[key]:
            out[key] = body[m.end():end].strip()
        else:  # an unknown heading, or a second copy of a known one: keep the whole section
            extra.append(body[m.start():end].strip())
    if heads[0].start() > 0 and body[:heads[0].start()].strip():
        extra.insert(0, body[:heads[0].start()].strip())
    out["extra_body"] = "\n\n".join(x for x in extra if x)
    if not out["system_rule"] and not out["legal_text"]:
        out["system_rule"] = body.strip()
        out["extra_body"] = ""
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
        trigger=str(meta.get("trigger") or "promotion").strip().lower(),
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
        extra_body=sections["extra_body"],
        path=path,
    )


def load_pack(settings: Settings, pack_id: str = "mccs") -> RulePack:
    """Read rules/<pack>/: PACK.md (products + pack metadata) and every other .md file as a rule."""
    d = pack_dir(settings, pack_id)
    if not (d / PACK_FILE).exists():
        return RulePack(id=pack_id, path=d)
    post = frontmatter.load(d / PACK_FILE)
    meta = dict(post.metadata)
    products = [RuleProduct(**{**{"id": "", "name": ""}, **p}) for p in (meta.get("products") or [])]
    rules = [parse_rule(f, pack_id) for f in sorted(d.glob("*.md")) if f.name != PACK_FILE]
    promotion = {str(k): [str(x) for x in (v or [])] for k, v in (meta.get("promotion") or {}).items()}
    return RulePack(id=str(meta.get("id") or pack_id), name=str(meta.get("name") or pack_id), description=str(meta.get("description") or ""),
                    sources=[str(x) for x in (meta.get("sources") or [])], promotion=promotion, products=products, body=post.content.strip(),
                    rules=sorted(rules, key=lambda r: (r.clause, r.id)), path=d)


_cache: dict[tuple[str, str], tuple[tuple, float, RulePack]] = {}  # (rules_dir, pack) -> (stamp, checked_at, pack)
_STAMP_TTL = 2.0  # seconds between stat() sweeps; RULES_DIR is an SMB share in the container, so they are not free


def _stamp(d: Path) -> tuple:
    """Changes when any rule file is written, added or removed (a file count alone misses an edit, a max mtime
    alone misses a deletion made outside delete_rule - a git checkout or a restore on the share)."""
    if not d.exists():
        return (0, 0.0)
    mtimes = [f.stat().st_mtime for f in d.glob("*.md")]
    return (len(mtimes), max(mtimes, default=0.0))


def active_pack(settings: Settings, pack_id: str = "mccs") -> RulePack:
    """load_pack() cached on the folder's file count + newest mtime, so an edit from Studio, the CLI or the share
    applies without a restart. The stamp itself is re-checked at most every _STAMP_TTL seconds."""
    d = pack_dir(settings, pack_id)
    key = (str(rules_dir(settings)), pack_id)  # two Settings in one process must not share an entry
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[1] < _STAMP_TTL:
        return hit[2]
    stamp = _stamp(d)
    if hit is not None and hit[0] == stamp:
        _cache[key] = (stamp, now, hit[2])
        return hit[2]
    pack = load_pack(settings, pack_id)
    _cache[key] = (stamp, now, pack)
    return pack


def forget(settings: Settings, pack_id: str) -> None:
    """Drop the cached pack after our own write, so the next read is immediate rather than TTL-delayed."""
    _cache.pop((str(rules_dir(settings)), pack_id), None)


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
    if rule.trigger not in TRIGGERS:
        errors.append(f"trigger '{rule.trigger}' must be one of {', '.join(TRIGGERS)}")
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


def is_promotional(pack: RulePack, text: str) -> bool:
    """Is this answer advertising - does it offer, recommend or detail the product rather than just mention it?

    The regulator's rules govern การโฆษณา, so a definition, a comparison of concepts or a 'no details yet' reply is
    not one. `promotion.signals` in PACK.md say what selling looks like; `promotion.exclude` are our own no-information
    sentences, which only veto when the answer quotes no figures at all (a refusal that still names a rate is an ad).
    With no signals configured the gate is open, which is the old every-mention behaviour."""
    signals = pack.promotion.get("signals") or []
    if not signals:
        return True
    if _any_pattern(text, signals) is None:
        return False
    if _any_pattern(text, pack.promotion.get("exclude") or []) and not re.search(r"\d", text):
        return False
    return True


def rules_for_products(pack: RulePack, product_ids: Iterable[str], *, status: str = "active") -> list[RuleSpec]:
    wanted = set(product_ids)
    return [r for r in pack.rules if (not status or r.status == status) and wanted & set(r.products)]


# ---------------- prompt blocks ----------------
HEADER = "# Responsible Lending (ปฏิบัติตามก่อนตอบ / mandatory before you answer)"
INTRO = (
    "These rules come from {sources}. They apply to what YOU write, not only to marketing material: an answer that "
    "OFFERS or RECOMMENDS a regulated product below - quotes its rate, fee, instalment or benefits, compares it, "
    "suggests it to the customer, or explains how to apply - is an advertisement under these rules.\n"
    "An answer that only defines a term, answers a general question, or says you have no details yet is NOT an "
    "advertisement. The wording you must never use applies to every answer.\n"
    "DO NOT WRITE THE WARNINGS YOURSELF. The app appends the exact mandated wording, in the customer's language, under "
    "the product name, after your answer. Your job is to make the answer itself compliant."
)
FOOTER = (
    "## How to comply\n"
    "- Never write a required warning, and never write your own version of one ('use it only when you need to', 'pay in "
    "full to avoid interest', 'borrow only what you can repay'). The app adds the official wording after your answer, so "
    "writing it yourself only makes the customer read it twice. End your answer with the product facts.\n"
    "- If you cannot state every figure a rule requires (reference rate and its date, effective rate range, calculation "
    "assumptions), do NOT quote an interest rate, a fee waiver or an instalment amount at all: describe the product "
    "qualitatively and tell the customer where the exact figures are confirmed.\n"
    "- Never use wording that encourages borrowing beyond need, promises approval without a credit check, or makes "
    "borrowing sound effortless, in any language.\n"
    "- Do not describe the warnings, mention that they are required, or refer to this section in the answer."
)


def _rule_lines(rule: RuleSpec, pack: RulePack, scope: Optional[set[str]] = None) -> str:
    pids = [p for p in rule.products if scope is None or p in scope]
    names = ", ".join((pack.product(p).name if pack.product(p) else p) for p in pids)
    when = ("whenever the answer mentions this product, selling or not" if rule.trigger == "mention"
            else "when the answer offers or recommends this product")
    out = [f"### {rule.clause or rule.id} — {rule.title}", f"Applies to: {names} ({when})",
           rule.system_rule.strip(), rule.assistant_note.strip()]
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
    required = [f'  - {product_label(pack, r.products, "th")}: "{r.phrases[0]}"'
                for r in rules if r.check == "required_phrase" and r.phrases]
    return "\n".join([
        HEADER,
        INTRO.format(sources="; ".join(pack.sources) or pack.name),
        "",
        "## Regulated product families (a question about any of them is a Responsible Lending answer)",
        *lines,
        "",
        "## Before you relay a specialist's answer",
        "- DO NOT ADD THE WARNINGS YOURSELF and do not write your own version of one. The app appends the exact mandated"
        " wording, in the customer's language, under the product name, after the answer you send. Relay the specialist's"
        " answer and stop at the product facts.",
        "- If the specialist's answer already contains a warning in its own words, leave it out of what you relay: the"
        " official wording is added for you, and two versions of the same warning read as a mistake.",
        *(["- For reference, the wording the app appends (never type it yourself):", *required] if required else []),
        "- If the answer quotes an interest rate, a fee waiver or an instalment amount without the assumptions and the"
        " reference-rate date the rules require, drop the figure from the answer and point the customer to the bank for"
        " the exact terms rather than sending an incomplete disclosure.",
        "- Never relay wording that encourages borrowing beyond need, promises approval without a credit check, or makes"
        " borrowing sound effortless.",
    ]).strip()


# ---------------- answer-time guard ----------------
def evaluate(pack: RulePack, text: str, *, question: str = "", skill_id: str = "") -> tuple[list[RuleFinding], list[str]]:
    """Check a drafted answer against the pack. Returns (findings, detected product ids)."""
    pids = detect_products(pack, text, skill_id=skill_id)  # what the ANSWER is about; the question is not the ad
    findings: list[RuleFinding] = []
    if not pids:
        return findings, pids
    promotional = is_promotional(pack, text)
    norm = normalize(text)
    for rule in rules_for_products(pack, pids):
        f = RuleFinding(rule_id=rule.id, clause=rule.clause, title=rule.title, severity=rule.severity,
                        products=sorted(set(rule.products) & set(pids)))
        if rule.trigger == "promotion" and not promotional:
            f.verdict, f.detail = "not_applicable", "the answer mentions the product but does not offer or recommend it"
        elif rule.applies_when and _any_pattern(text, rule.applies_when) is None:
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


def product_label(pack: RulePack, product_ids: Iterable[str], language: str) -> str:
    """How the warning block names the product it is about, so a customer knows which one it applies to."""
    names = []
    for pid in product_ids:
        p = pack.product(pid)
        if p is None:
            names.append(pid)
        elif language == "en":
            names.append(p.label_en or p.label or p.name)
        else:
            names.append(p.label or p.name)
    return " / ".join(dict.fromkeys(names))  # two families with the same short label read as one


def apply_disclosures(text: str, rules: list[RuleSpec], language: str, labels: Optional[dict[str, str]] = None) -> tuple[str, str, list[str]]:
    """Append the mandated warnings that are missing, verbatim, as a block at the end of the answer.

    Returns (answer, appended block, ids of the rules whose wording was actually added). The answer is always
    `text + block`, never a rewrite of `text`: a streaming caller has already sent `text` to the customer, so the
    stored answer has to keep it byte for byte or the two diverge."""
    norm = normalize(text)
    seen: set[str] = set()
    groups: dict[str, list[str]] = {}  # product label -> warnings, so each block says which product it is about
    added: list[str] = []  # rule ids whose wording really went in - only these count as fixed
    for r in rules:
        d = disclosure_text(r, language)
        if d and not _contains(norm, d) and normalize(d) not in seen:
            seen.add(normalize(d))
            groups.setdefault((labels or {}).get(r.id, ""), []).append(d)
            added.append(r.id)
    if not added:
        return text, "", []
    parts = []
    for label, warnings in groups.items():
        parts.append((f"**{label}**\n" if label else "") + "\n".join(f"⚠️ {w}" for w in warnings))
    block = "\n\n---\n" + "\n\n".join(parts)
    return text + block, block, added


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
    labels = {f.rule_id: product_label(pack, f.products, language) for f in findings}
    fixed_text, appended, fixed_ids = apply_disclosures(text, to_append, language, labels)
    for f in findings:
        if f.rule_id in fixed_ids:  # only the rules whose wording really went into the answer
            f.fixed, f.verdict, f.detail = True, "compliant", f"{f.detail}; the required wording was added to the answer"
    report = {
        "pack": pack.id,
        "products": pids,
        "product_names": [pack.product(p).name for p in pids if pack.product(p)],
        "checked": len(findings),
        "fixed": sorted(fixed_ids),
        "violations": [f.rule_id for f in findings if f.verdict == "non_compliant"],
        "review": [f.rule_id for f in findings if f.verdict == "undefined"],
        "findings": [f.model_dump() for f in findings],
    }
    return fixed_text, {**report, "appended": appended}


# ---------------- editing (Studio / CLI) ----------------
FRONT_KEYS = ("id", "pack", "title", "regulation", "clause", "products", "status", "trigger", "severity", "check", "enforcement",
              "phrases", "patterns", "applies_when", "disclosure", "template")


def rule_path(settings: Settings, pack_id: str, rule_id: str) -> Path:
    if not ID_RE.match(rule_id or ""):
        raise ValueError(f"invalid rule id '{rule_id}'")
    return pack_dir(settings, pack_id) / f"{rule_id}.md"


def dump_rule(rule: RuleSpec) -> str:
    meta = {k: getattr(rule, k) for k in FRONT_KEYS}
    meta["products"] = sorted(meta["products"])  # canonical order: the sheet and the Studio checkboxes disagree,
    meta["phrases"] = list(meta["phrases"])      # and an order-only difference would rewrite files and re-version agents
    meta = {k: v for k, v in meta.items() if v not in ("", [], {}, None)}
    body = f"## กฎหมาย (legal text)\n{rule.legal_text}\n\n## กฎสำหรับระบบ (system rule)\n{rule.system_rule}\n"
    if rule.assistant_note:
        body += f"\n## หมายเหตุสำหรับผู้ช่วย (assistant note)\n{rule.assistant_note}\n"
    if rule.extra_body:  # sections we do not understand are written back untouched
        body += f"\n{rule.extra_body}\n"
    return frontmatter.dumps(frontmatter.Post(body, **meta), sort_keys=False, allow_unicode=True, width=100000) + "\n"


def write_rule(settings: Settings, pack_id: str, rule: RuleSpec) -> RuleSpec:
    p = rule_path(settings, pack_id, rule.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_rule(rule), encoding="utf-8")
    forget(settings, pack_id)
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
    for k in ("legal_text", "system_rule", "assistant_note", "extra_body"):
        if k in form:
            data[k] = str(form[k])
    updated = RuleSpec(**{**data, "id": rule_id, "pack": pack_id})
    errors, _ = validate_rule(updated, active_pack(settings, pack_id))
    if errors:
        raise ValueError("; ".join(errors))
    return write_rule(settings, pack_id, updated)


def create_rule(settings: Settings, pack_id: str, form: dict) -> RuleSpec:
    """A rule written by hand in Studio (not every rule has to come from an MCCS export)."""
    rule_id = str(form.get("id", "")).strip()
    if not ID_RE.match(rule_id):
        raise ValueError("id must be lowercase letters, digits and dashes")
    if rule_path(settings, pack_id, rule_id).exists():
        raise FileExistsError(f"rule '{rule_id}' already exists in pack '{pack_id}'")
    data = {k: v for k, v in form.items() if k in FRONT_KEYS or k in ("legal_text", "system_rule", "assistant_note", "extra_body")}
    rule = RuleSpec(**{**data, "id": rule_id, "pack": pack_id})
    errors, _ = validate_rule(rule, active_pack(settings, pack_id))
    if errors:
        raise ValueError("; ".join(errors))
    return write_rule(settings, pack_id, rule)


# ---- product families (PACK.md) ----
PRODUCT_KEYS = ("id", "name", "label", "label_en", "aliases", "skills", "match")


def write_products(settings: Settings, pack_id: str, products: list[RuleProduct]) -> RulePack:
    """Replace the product taxonomy in PACK.md, keeping the rest of the file (metadata + body) untouched."""
    p = pack_dir(settings, pack_id) / PACK_FILE
    if not p.exists():
        raise FileNotFoundError(f"pack '{pack_id}' does not exist")
    post = frontmatter.load(p)
    post.metadata["products"] = [x.model_dump(include=set(PRODUCT_KEYS)) for x in products]
    p.write_text(frontmatter.dumps(post, sort_keys=False, allow_unicode=True, width=100000) + "\n", encoding="utf-8")
    forget(settings, pack_id)
    return active_pack(settings, pack_id)


def upsert_product(settings: Settings, pack_id: str, product_id: str, form: dict) -> RuleProduct:
    """Create or update one product family: its name, aliases, detection patterns and the skills that carry its rules."""
    if not ID_RE.match(product_id):
        raise ValueError(f"invalid product id '{product_id}'")
    pack = active_pack(settings, pack_id)
    listy = lambda k: [str(x).strip() for x in (form.get(k) or []) if str(x).strip()]  # noqa: E731
    current = pack.product(product_id)
    updated = RuleProduct(
        id=product_id,
        name=str(form.get("name") or (current.name if current else "")).strip(),
        label=str(form.get("label", current.label if current else "")).strip(),
        label_en=str(form.get("label_en", current.label_en if current else "")).strip(),
        aliases=listy("aliases") if "aliases" in form else (current.aliases if current else []),
        skills=listy("skills") if "skills" in form else (current.skills if current else []),
        match=listy("match") if "match" in form else (current.match if current else []),
    )
    if not updated.name:
        raise ValueError("name is required (it is how the rules sheet refers to this family)")
    for pattern in updated.match:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"bad regex {pattern!r}: {e}") from e
    products = [updated if x.id == product_id else x for x in pack.products]
    if current is None:
        products.append(updated)
    write_products(settings, pack_id, products)
    return updated


def delete_product(settings: Settings, pack_id: str, product_id: str) -> None:
    pack = active_pack(settings, pack_id)
    if pack.product(product_id) is None:
        raise FileNotFoundError(f"product '{product_id}' does not exist in pack '{pack_id}'")
    used = [r.id for r in pack.rules if product_id in r.products]
    if used:
        raise ValueError(f"still used by {len(used)} rule(s): {', '.join(used[:5])}")
    write_products(settings, pack_id, [x for x in pack.products if x.id != product_id])


def delete_rule(settings: Settings, pack_id: str, rule_id: str) -> None:
    p = rule_path(settings, pack_id, rule_id)
    if not p.exists():
        raise FileNotFoundError(f"rule '{rule_id}' does not exist in pack '{pack_id}'")
    p.unlink()
    forget(settings, pack_id)
