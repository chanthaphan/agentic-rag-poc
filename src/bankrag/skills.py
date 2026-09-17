"""Skill folders: skills/<id>/SKILL.md with YAML frontmatter + markdown instructions."""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path

import frontmatter

from .models import SkillSpec

SKILL_FILE = "SKILL.md"
BASE_ID = "_base"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def parse_skill(path: Path) -> SkillSpec:
    post = frontmatter.load(path / SKILL_FILE)
    meta = dict(post.metadata)
    keywords = meta.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    filt = meta.get("filter", None)
    if filt is not None:
        filt = str(filt)
    suggestions = meta.get("suggestions") or []
    if isinstance(suggestions, str):
        suggestions = [x.strip() for x in suggestions.splitlines() if x.strip()]
    tools = meta.get("tools") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    return SkillSpec(
        id=str(meta.get("id") or path.name),
        name=str(meta.get("name") or path.name),
        description=" ".join(str(meta.get("description", "")).split()),
        product_category=str(meta.get("product_category") or path.name),
        keywords=[str(k) for k in keywords],
        model=meta.get("model"),
        top_k=int(meta.get("top_k", 5)),
        filter=filt,
        version=int(meta.get("version", 1)),
        tools=[str(t) for t in tools],
        suggestions=[str(x) for x in suggestions],
        body=post.content.strip(),
        path=path,
    )


def load_base(skills_dir: Path) -> str:
    p = skills_dir / BASE_ID / SKILL_FILE
    if not p.exists():
        return ""
    return frontmatter.load(p).content.strip()


def load_skills(skills_dir: Path) -> dict[str, SkillSpec]:
    skills: dict[str, SkillSpec] = {}
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        if not (d / SKILL_FILE).exists():
            continue
        spec = parse_skill(d)
        skills[spec.id] = spec
    return skills


def validate_skill(spec: SkillSpec, knowledge_dir: Path | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not ID_RE.match(spec.id):
        errors.append(f"id '{spec.id}' must match {ID_RE.pattern}")
    if spec.path is not None and spec.path.name != spec.id:
        errors.append(f"id '{spec.id}' must equal folder name '{spec.path.name}'")
    if not spec.description:
        errors.append("description is required (used by the router)")
    if not spec.keywords:
        warnings.append("keywords are empty; routing will rely on the description only")
    if not spec.body:
        warnings.append("SKILL.md has no instructions body")
    if spec.top_k < 1 or spec.top_k > 50:
        errors.append("top_k must be between 1 and 50")
    if spec.product_category not in ("all", "*", "") and knowledge_dir is not None:
        if not (knowledge_dir / spec.product_category).exists():
            warnings.append(f"knowledge/{spec.product_category} does not exist yet (no documents for this skill)")
    if len(spec.suggestions) < 3:
        warnings.append("fewer than 3 suggestions; the app pads follow-up prompts from the general skill")
    if spec.filter is not None and spec.filter and "product_category" not in spec.filter:
        warnings.append("custom filter does not restrict product_category; the skill will search across categories")
    return errors, warnings


PERSONA_PLACEHOLDERS = ("{assistant_name}", "{assistant_name_en}")


def personalize(text: str, names: dict[str, str] | None) -> str:
    """Fill the persona placeholders ({assistant_name}, {assistant_name_en}) so the name lives in one setting, not in
    every prompt file. Plain replacement, not str.format: prompts are free to contain other braces."""
    for key, value in (names or {}).items():
        text = text.replace("{" + key + "}", value)
    return text


def compose_instructions(base_body: str, spec: SkillSpec, rules_block: str = "", names: dict[str, str] | None = None) -> str:
    """base rules + the skill's own instructions + the Responsible Lending rules that cover its products (last, so the
    agent reads the compliance obligations right before it answers), with the persona name filled in."""
    parts = []
    if base_body:
        parts.append(base_body)
    parts.append(f"# Skill: {spec.name} (id: {spec.id})\n\n{spec.body}")
    if rules_block:
        parts.append(rules_block)
    return personalize("\n\n---\n\n".join(parts).strip() + "\n", names)


def install_skill_zip(data: bytes | Path, skills_dir: Path, overwrite: bool = True) -> SkillSpec:
    """Install a skill from a zip that contains SKILL.md at its root or inside one top-level folder."""
    raw = data.read_bytes() if isinstance(data, Path) else data
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        for n in names:
            p = Path(n)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"unsafe path in zip: {n}")
        skill_md = [n for n in names if Path(n).name == SKILL_FILE]
        if len(skill_md) != 1:
            raise ValueError("zip must contain exactly one SKILL.md")
        prefix = str(Path(skill_md[0]).parent)
        prefix = "" if prefix == "." else prefix.rstrip("/") + "/"
        post = frontmatter.loads(zf.read(skill_md[0]).decode("utf-8"))
        skill_id = str(post.metadata.get("id") or (Path(prefix).name if prefix else "")).strip()
        if not ID_RE.match(skill_id):
            raise ValueError(f"invalid or missing skill id '{skill_id}' in frontmatter")
        target = skills_dir / skill_id
        if target.exists():
            if not overwrite:
                raise FileExistsError(f"skill '{skill_id}' already exists")
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for n in names:
            if prefix and not n.startswith(prefix):
                continue
            rel = n[len(prefix):] if prefix else n
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(n))
    return parse_skill(target)


def skill_to_zip(spec: SkillSpec) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        assert spec.path is not None
        for f in sorted(spec.path.rglob("*")):
            if f.is_file():
                zf.write(f, str(f.relative_to(spec.path)))
    return buf.getvalue()


# ---- editing (Studio) ----
FORM_KEYS = ("name", "id", "description", "product_category", "keywords", "model", "top_k", "filter", "version", "tools", "suggestions")
SKILL_TEMPLATE_BODY = """## Role
You are the {name} specialist. Your knowledge base holds Bangkok Bank documents for this product family.

## How to answer
- Quote figures and conditions exactly as documented; never estimate fees, rates or coverage.
- If no documents are available yet for this category, say so and point to the bank's website or hotline.
"""


def _clean_form(meta: dict) -> dict:
    out: dict = {}
    for k in FORM_KEYS:
        if k not in meta or meta[k] is None:
            continue
        v = meta[k]
        if k in ("keywords", "suggestions", "tools"):
            if isinstance(v, str):
                v = [x.strip() for x in re.split(r"\n" if k == "suggestions" else r"[\n,]", v) if x.strip()]
            v = [str(x).strip() for x in v if str(x).strip()]
        elif k in ("top_k", "version"):
            v = int(v)
        elif k == "filter":
            v = None if v == "" and meta.get("filter") is None else str(v)
        else:
            v = str(v).strip()
        if v == "" and k in ("model",):
            continue
        out[k] = v
    return out


def write_skill(skills_dir: Path, skill_id: str, meta: dict, body: str) -> SkillSpec:
    """Update SKILL.md frontmatter (known keys, order preserved) and body. Returns the re-parsed spec."""
    if not ID_RE.match(skill_id) or skill_id == BASE_ID:
        raise ValueError(f"invalid skill id '{skill_id}'")
    path = skills_dir / skill_id / SKILL_FILE
    if not path.exists():
        raise FileNotFoundError(f"skill '{skill_id}' does not exist")
    post = frontmatter.load(path)
    for k, v in _clean_form(meta).items():
        if k == "id":
            continue
        post.metadata[k] = v
    post.metadata["id"] = skill_id
    post.content = body.strip() + "\n"
    path.write_text(frontmatter.dumps(post, sort_keys=False, allow_unicode=True) + "\n", encoding="utf-8")
    return parse_skill(path.parent)


def create_skill(skills_dir: Path, form: dict) -> SkillSpec:
    skill_id = str(form.get("id", "")).strip()
    if not ID_RE.match(skill_id) or skill_id == BASE_ID:
        raise ValueError("id must be lowercase letters, digits and dashes")
    target = skills_dir / skill_id
    if target.exists():
        raise FileExistsError(f"skill '{skill_id}' already exists")
    meta = _clean_form(form)
    ordered = {
        "name": meta.get("name") or skill_id.replace("-", " ").title(),
        "id": skill_id,
        "description": meta.get("description") or f"{skill_id} products",
        "product_category": meta.get("product_category") or skill_id,
        "keywords": meta.get("keywords") or [skill_id],
        "model": meta.get("model") or "gpt-4.1-mini",
        "top_k": meta.get("top_k", 5),
        "version": 1,
        "suggestions": meta.get("suggestions") or [],
    }
    body = (form.get("body") or "").strip() or SKILL_TEMPLATE_BODY.format(name=ordered["name"])
    target.mkdir(parents=True)
    post = frontmatter.Post(body + "\n", **ordered)
    (target / SKILL_FILE).write_text(frontmatter.dumps(post, sort_keys=False, allow_unicode=True) + "\n", encoding="utf-8")
    return parse_skill(target)


def delete_skill(skills_dir: Path, skill_id: str) -> None:
    if not ID_RE.match(skill_id) or skill_id == BASE_ID:
        raise ValueError(f"invalid skill id '{skill_id}'")
    target = (skills_dir / skill_id).resolve()
    if target.parent != skills_dir.resolve() or not target.exists():
        raise FileNotFoundError(f"skill '{skill_id}' does not exist")
    shutil.rmtree(target)


# ---- base rules ----
def read_base(skills_dir: Path) -> dict:
    p = skills_dir / BASE_ID / SKILL_FILE
    post = frontmatter.load(p) if p.exists() else frontmatter.Post("")
    return {"frontmatter": dict(post.metadata), "body": post.content.strip()}


def write_base(skills_dir: Path, body: str) -> dict:
    body = body.strip()
    if "knowledge base" not in body.lower() and "knowledge_base_retrieve" not in body:
        raise ValueError("the base rules must keep the grounding section that tells agents to use the knowledge base")
    p = skills_dir / BASE_ID / SKILL_FILE
    post = frontmatter.load(p) if p.exists() else frontmatter.Post("", name="Base rules", id=BASE_ID)
    post.content = body + "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(frontmatter.dumps(post, sort_keys=False, allow_unicode=True) + "\n", encoding="utf-8")
    return read_base(skills_dir)


# ---- lint ----
def _lang(text: str) -> str:
    thai = sum(1 for ch in text if "\u0e00" <= ch <= "\u0e7f")
    return "th" if thai else "en"


def lint_skills(skills: dict[str, SkillSpec], knowledge_dir: Path | None = None, deployed_models: list[str] | None = None, has_docs: dict[str, bool] | None = None) -> dict[str, list[dict]]:
    """Per-skill findings: [{level: warn|info, code, message}]. Cross-skill keyword overlap hurts routing."""
    out: dict[str, list[dict]] = {sid: [] for sid in skills}
    kw_index: dict[str, list[str]] = {}
    for sid, spec in skills.items():
        for k in spec.keywords:
            kw_index.setdefault(k.strip().lower(), []).append(sid)
    for sid, spec in skills.items():
        f = out[sid]
        shared = sorted({k for k in (x.strip().lower() for x in spec.keywords) if len(kw_index.get(k, [])) > 1})
        if shared:
            others = sorted({o for k in shared for o in kw_index[k] if o != sid})
            f.append({"level": "warn", "code": "keyword-overlap", "message": f"{len(shared)} keyword(s) also used by {', '.join(others)}: {', '.join(shared[:8])}{'…' if len(shared) > 8 else ''}"})
        if len(spec.description) < 80:
            f.append({"level": "warn", "code": "short-description", "message": "description under 80 characters; the router relies on it"})
        th = [x for x in spec.suggestions if _lang(x) == "th"]
        en = [x for x in spec.suggestions if _lang(x) == "en"]
        if len(th) < 3 or len(en) < 3:
            f.append({"level": "warn", "code": "suggestions-language", "message": f"suggestions: {len(th)} Thai / {len(en)} English (need 3 each for language-matched follow-ups)"})
        if spec.product_category not in ("all", "*", ""):
            docs_present = has_docs.get(sid) if has_docs and sid in has_docs else (knowledge_dir is not None and (knowledge_dir / spec.product_category).exists())
            if not docs_present:
                f.append({"level": "info", "code": "no-documents", "message": f"no documents in knowledge/{spec.product_category}: the agent has no retrieval tool and answers 'not in the knowledge base'"})
        if deployed_models is not None and spec.model and spec.model not in deployed_models:
            f.append({"level": "warn", "code": "model-not-deployed", "message": f"model '{spec.model}' is not deployed in the Foundry project"})
        body_l = spec.body.lower()
        if "how to answer" not in body_l and "## " not in spec.body:
            f.append({"level": "info", "code": "no-structure", "message": "instructions have no sections; add '## Role' and '## How to answer'"})
        if not spec.keywords:
            f.append({"level": "warn", "code": "no-keywords", "message": "no keywords; routing relies on the description only"})
    return out
