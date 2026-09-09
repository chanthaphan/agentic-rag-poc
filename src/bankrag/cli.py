"""bankrag command line."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from .config import ConfigError, Settings

app = typer.Typer(help="Bangkok Bank product agent POC: Foundry agents + Azure AI Search (Foundry IQ)", no_args_is_help=True)
setup_app = typer.Typer(help="Provision search index, knowledge bases, project connections")
skills_app = typer.Typer(help="Validate, list and sync skills to Foundry")
eval_app = typer.Typer(help="Evaluate routing and answers")
rules_app = typer.Typer(help="Responsible Lending rules: list, validate, import from / export to xlsx, check an answer")
services_app = typer.Typer(help="Live Bangkok Bank services (FX rates, branch locator): probe the API and check the key")
app.add_typer(setup_app, name="setup")
app.add_typer(skills_app, name="skills")
app.add_typer(eval_app, name="eval")
app.add_typer(rules_app, name="rules")
app.add_typer(services_app, name="services")
console = Console()


def _settings() -> Settings:
    return Settings.load()


def _skills(settings: Settings):
    from .skills import load_base, load_skills

    return load_skills(settings.skills_dir), load_base(settings.skills_dir)


# ---------------- setup ----------------
@setup_app.command("index")
def setup_index(delete: bool = typer.Option(False, help="delete and recreate the index (drops all documents)")):
    """Create or update the search index (fields, vectorizer, semantic config)."""
    from . import search_index as SI

    s = _settings()
    if delete:
        SI.delete_index(s)
        rprint(f"[yellow]deleted index {s.search_index}[/]")
    idx = SI.ensure_index(s)
    rprint(f"[green]index '{idx.name}' ready[/] with {len(idx.fields)} fields, vectorizer -> {s.embed_deployment}")


@setup_app.command("kb")
def setup_kb(skill: Optional[str] = typer.Option(None, help="only this skill id")):
    """Create or update knowledge sources and knowledge bases for every skill."""
    from . import knowledge_base as KB
    from .foundry_sync import plan_kb_owners

    s = _settings()
    skills, _ = _skills(s)
    owners = plan_kb_owners(s, skills)
    for spec in skills.values():
        if skill and spec.id != skill:
            continue
        if owners[spec.id].id != spec.id:
            rprint(f"[yellow]{spec.id}[/]: no documents for '{spec.product_category}' yet -> shares {owners[spec.id].kb_name} (Free tier allows 3 knowledge sources)")
            continue
        ks, kb = KB.ensure_knowledge_objects(s, spec)
        rprint(f"[green]{spec.id}[/]: knowledge source {ks}, knowledge base {kb} (filter: {spec.effective_filter or '(none)'})")


@setup_app.command("connections")
def setup_connections(skill: Optional[str] = typer.Option(None, help="only this skill id")):
    """Create the RemoteTool project connections (project managed identity -> KB MCP endpoint)."""
    from . import connections as CONN
    from .foundry import credential
    from .foundry_sync import plan_kb_owners

    s = _settings()
    skills, _ = _skills(s)
    owners = plan_kb_owners(s, skills)
    for spec in skills.values():
        if skill and spec.id != skill:
            continue
        if owners[spec.id].id != spec.id:
            rprint(f"[yellow]{spec.id}[/]: shares {owners[spec.id].connection_name}")
            continue
        res = CONN.ensure_kb_connection(s, spec, credential())
        rprint(f"[green]{spec.id}[/]: connection {res.get('name')} -> {res.get('properties', {}).get('target')}")


@setup_app.command("all")
def setup_all():
    """index + kb + connections + skills sync."""
    setup_index(delete=False)
    setup_kb(skill=None)
    if _settings().kb_mcp_auth != "apikey":
        setup_connections(skill=None)
    skills_sync(only=None, prune=False, keep=0, register_native=False)


# ---------------- skills ----------------
@skills_app.command("validate")
def skills_validate():
    from .skills import validate_skill

    s = _settings()
    skills, _ = _skills(s)
    bad = 0
    for spec in skills.values():
        errors, warnings = validate_skill(spec, s.knowledge_dir)
        bad += len(errors)
        for e in errors:
            rprint(f"[red]{spec.id}: {e}[/]")
        for w in warnings:
            rprint(f"[yellow]{spec.id}: {w}[/]")
        if not errors:
            rprint(f"[green]{spec.id}: ok[/] ({len(spec.keywords)} keywords, filter: {spec.effective_filter or '(none)'})")
    raise typer.Exit(code=1 if bad else 0)


@skills_app.command("list")
def skills_list(remote: bool = typer.Option(True, help="compare with Foundry agent versions")):
    from .foundry_sync import status

    s = _settings()
    skills, base = _skills(s)
    rows = status(s, skills, base) if remote else [
        {"id": k.id, "name": k.name, "product_category": k.product_category, "model": k.model or s.default_chat_model, "agent": k.agent_name, "state": "-", "version": ""}
        for k in skills.values()
    ]
    t = Table("id", "name", "category", "model", "agent", "state", "version")
    for r in rows:
        t.add_row(r["id"], r["name"], r["product_category"], r["model"], r["agent"], r["state"], str(r["version"]))
    console.print(t)


@skills_app.command("sync")
def skills_sync(
    only: Optional[str] = typer.Option(None, help="sync a single skill id"),
    prune: bool = typer.Option(False, help="delete Foundry agents/KBs for skills whose folder was removed"),
    keep: int = typer.Option(0, help="keep only the N newest agent versions (0 = keep all)"),
    register_native: bool = typer.Option(False, help="also publish as native Foundry Skills (beta)"),
):
    """Create/update knowledge sources, knowledge bases, connections and agent versions for every skill."""
    from .foundry_sync import sync_skills

    s = _settings()
    skills, base = _skills(s)
    report = sync_skills(s, skills, base, only=only, prune=prune, keep=keep, register_native=register_native, log=lambda m: rprint(f"[dim]{m}[/]"))
    t = Table("skill", "knowledge base", "connection", "agent", "action", "version", "note")
    for r in report.rows:
        color = {"created": "green", "updated": "green", "unchanged": "cyan", "error": "red", "pruned": "yellow"}.get(r.action, "white")
        t.add_row(r.skill_id, r.knowledge_base, r.connection, r.agent, f"[{color}]{r.action}[/]", r.version, r.note[:80])
    console.print(t)


@skills_app.command("install")
def skills_install(zip_path: Path):
    """Install a skill from a zip (SKILL.md at the root or in one folder)."""
    from .skills import install_skill_zip, validate_skill

    s = _settings()
    spec = install_skill_zip(zip_path, s.skills_dir)
    errors, warnings = validate_skill(spec, s.knowledge_dir)
    rprint(f"installed skill [green]{spec.id}[/] -> {spec.path}")
    for w in warnings:
        rprint(f"[yellow]{w}[/]")
    for e in errors:
        rprint(f"[red]{e}[/]")


# ---------------- knowledge ----------------
@app.command("seed")
def seed(
    src: Path = typer.Option(None, "--from", help="crawler data/products/th-TH/Personal/Cards folder"),
    include_promotions: bool = typer.Option(False),
    clear: bool = typer.Option(False, help="remove knowledge/credit-card first"),
):
    """Seed knowledge/credit-card from the bblwebsite_crawler output."""
    from .seed import DEFAULT_CRAWLER_DATA, seed_credit_cards

    s = _settings()
    seed_credit_cards(src or DEFAULT_CRAWLER_DATA, s.knowledge_dir, include_promotions=include_promotions, clear=clear)


@app.command("ingest")
def ingest_cmd(
    category: Optional[str] = typer.Option(None, help="only this product category folder"),
    full: bool = typer.Option(False, help="re-embed everything even if unchanged"),
    dry_run: bool = typer.Option(False, help="only report what would change"),
):
    """Chunk, embed and upload knowledge/<category>/** into the search index."""
    from . import search_index as SI
    from .ingest.pipeline import ingest

    s = _settings()
    report = ingest(s, category=category, full=full, dry_run=dry_run, log=lambda m: rprint(f"[dim]{m}[/]"))
    t = Table("action", "count")
    for k, v in report.summary().items():
        t.add_row(k, str(v))
    console.print(t)
    if not dry_run:
        try:
            rprint("index facets:", SI.facet_counts(s))
            st = SI.index_stats(s)
            rprint(f"index documents: {st.get('document_count')}, storage bytes: {st.get('storage_size')}, vector bytes: {st.get('vector_index_size')}")
        except Exception as e:  # noqa: BLE001
            rprint(f"[yellow]could not read index stats: {e}[/]")


@app.command("retrieve")
def retrieve_cmd(question: str, skill: str = typer.Option("credit-card"), max_docs: int = typer.Option(5)):
    """Query a skill's knowledge base directly (no agent) and print references."""
    from . import knowledge_base as KB

    s = _settings()
    skills, _ = _skills(s)
    from .foundry_sync import synced_kb_owners

    spec = synced_kb_owners(s, skills)[skill]
    refs = KB.retrieve(s, spec.kb_name, question, ks_name=spec.ks_name, max_docs=max_docs)
    for r in refs:
        rprint(f"[bold]{r.title}[/] ({r.doc_type}, score={r.score}) {r.source_url}\n  {r.snippet[:200]}")
    if not refs:
        rprint("[yellow]no references returned[/]")


# ---------------- chat ----------------
@app.command("chat")
def chat_cmd(
    question: Optional[str] = typer.Argument(None, help="one question; omit for an interactive loop"),
    skill: Optional[str] = typer.Option(None, help="force a skill id"),
    debug: bool = typer.Option(False, help="print routing, tool calls and sources"),
):
    """Ask the agents (routes to a skill, answers with citations)."""
    from .chat import ChatSession

    s = _settings()
    skills, _ = _skills(s)
    session = ChatSession(s, skills)

    def show(ans):
        rprint(f"[cyan]skill={ans.skill_id} confidence={ans.confidence:.2f}[/] [dim]{ans.route_reason}[/]")
        rprint(ans.text)
        if ans.citations:
            rprint("[dim]citations:[/]", [c.url for c in ans.citations])
        if debug:
            for tc in ans.tool_calls:
                rprint(f"[dim]{json.dumps(tc, ensure_ascii=False)[:600]}[/]")
            for r in ans.references:
                rprint(f"[dim]source: {r.title} | {r.source_url} | score={r.score}[/]")

    if question:
        show(session.ask(question, force_skill=skill))
        return
    rprint("[dim]interactive chat; type /quit to exit, /reset to start a new conversation[/]")
    while True:
        q = console.input("[bold]you> [/]").strip()
        if not q:
            continue
        if q == "/quit":
            break
        if q == "/reset":
            session.reset()
            continue
        show(session.ask(q, force_skill=skill))


@app.command("serve")
def serve(port: Optional[int] = typer.Option(None), reload: bool = typer.Option(False)):
    """Run the FastAPI app + web page."""
    import uvicorn

    s = _settings()
    uvicorn.run("bankrag.api:app", host="127.0.0.1", port=port or s.api_port, reload=reload)


# ---------------- eval ----------------
@eval_app.command("routing")
def eval_routing(file: Optional[Path] = typer.Option(None, help="defaults to evals/routing_questions.yaml")):
    """Routing accuracy on a labelled question set (uses the router agent)."""
    import yaml

    from .evals import load_cases, run_routing
    from .sessions import save_eval_run

    s = _settings()
    skills, _ = _skills(s)
    cases = yaml.safe_load(file.read_text(encoding="utf-8")) if file else load_cases(s, "routing")
    run = run_routing(s, skills, cases, log=lambda m: rprint(("[green]" if m.startswith("ok") else "[red]") + m.replace("[", "\\[") + "[/]"))
    save_eval_run(s, run)
    rprint(f"\naccuracy: {run['summary']['passed']}/{run['summary']['questions']} = {run['summary']['accuracy']:.0%}  cost ${run['summary']['total_cost_usd']:.4f}  run {run['id']}")


@eval_app.command("rag")
def eval_rag(file: Optional[Path] = typer.Option(None, help="defaults to evals/rag_questions.yaml")):
    """Answer questions and check expected substrings + citation presence."""
    import yaml

    from .evals import load_cases, run_rag
    from .sessions import save_eval_run

    s = _settings()
    skills, _ = _skills(s)
    cases = yaml.safe_load(file.read_text(encoding="utf-8")) if file else load_cases(s, "rag")
    run = run_rag(s, skills, cases, log=lambda m: rprint(("[green]" if m.startswith("ok") else "[red]") + m.replace("[", "\\[") + "[/]"))
    save_eval_run(s, run)
    rprint(f"\npassed: {run['summary']['passed']}/{run['summary']['questions']}  cost ${run['summary']['total_cost_usd']:.4f}  run {run['id']}")


# ---------------- live services (FX, branches) ----------------
@services_app.command("probe")
def services_probe(lang: str = typer.Option("en", help="th | en")):
    """Hit each live endpoint with the configured key and print status + response shape.

    Run this once after setting BBL_API_KEY: it confirms the key works and reveals the real JSON shapes (which the
    normaliser is written defensively against). Paste the output back so the parsing and the branch search can be
    finalised. Runs on your machine, so it is not affected by the sandbox that blocks the agent from calling out."""
    import json as _json

    from . import services as SV

    s = _settings()
    if not SV.configured(s):
        rprint("[red]BBL_API_KEY is not set in .env[/]")
        raise typer.Exit(code=1)
    from datetime import date

    checks = [
        ("FX latest", lambda: SV.fx_latest_raw(s)),
        ("FX last update", lambda: SV.fx_last_update(s)),
        (f"FX today round 2 ({lang})", lambda: SV.fx_rates_raw(s, date.today(), 2, lang)),
        (f"Provinces ({lang})", lambda: SV.provinces(s, lang)),
        (f"Countries ({lang})", lambda: SV.countries(s, lang)),
        ("Branches near Bangkok", lambda: SV.search_places_raw(s, "กรุงเทพมหานคร", 13.697057591968564, 100.64558570030171)),
    ]
    for name, fn in checks:
        try:
            data = fn()
        except SV.ServiceError as e:
            rprint(f"[red]{name}: {e}[/]")
            continue
        preview = _json.dumps(data, ensure_ascii=False)
        if isinstance(data, list):
            shape = f"list[{len(data)}]; first item: {_json.dumps(data[0], ensure_ascii=False)[:300] if data else '-'}"
        elif isinstance(data, dict):
            shape = f"dict keys: {', '.join(list(data)[:10])}"
        else:
            shape = f"{type(data).__name__}: {preview[:120]}"
        rprint(f"[green]{name}: ok[/] {shape}")
    rates = []
    try:
        rates = SV.normalize_fx(SV.fx_latest_raw(s))
    except SV.ServiceError:
        pass
    if rates:
        rprint(f"\n[bold]normalised {len(rates)} currencies[/]; sample:")
        for r in rates[:3]:
            rprint(f"  {r.currency} {r.name or ''} buying={r.buying} selling={r.selling} unit={r.unit or '-'}")
        parsed = sum(1 for r in rates if r.buying is not None or r.selling is not None)
        rprint(f"[{'green' if parsed else 'yellow'}]{parsed}/{len(rates)} have a buying/selling number "
               f"({'parsing looks right' if parsed else 'field names differ - send the shape above so I can fix normalize_fx'})[/]")


@services_app.command("branches")
def services_branches(lat: float = typer.Argument(..., help="latitude, e.g. 13.6970"),
                      lon: float = typer.Argument(..., help="longitude, e.g. 100.6455"),
                      province: str = typer.Option("", help="Thai province name, e.g. กรุงเทพมหานคร"),
                      limit: int = typer.Option(5)):
    """Branches nearest a pair of coordinates, exactly as the find_branch tool would return them."""
    import json as _json

    from . import services as SV

    rprint(_json.dumps(SV.find_branch(_settings(), lat, lon, province=province, limit=limit), ensure_ascii=False, indent=1))


@services_app.command("fx")
def services_fx(currency: str = typer.Argument(..., help="ISO code, e.g. USD"), lang: str = typer.Option("en")):
    """The latest rate for one currency, exactly as the fx_rate tool would return it."""
    import json as _json

    from . import services as SV

    rprint(_json.dumps(SV.fx_rate(_settings(), currency, lang=lang), ensure_ascii=False, indent=1))


# ---------------- responsible lending rules ----------------
@rules_app.command("list")
def rules_list(pack: str = typer.Option("mccs", help="rule pack folder under rules/")):
    """Every rule in the pack: which products it covers, how it is checked, and which skill agents carry it."""
    from . import rules as RL

    s = _settings()
    p = RL.active_pack(s, pack)
    if not p.rules:
        rprint(f"[yellow]no rules in rules/{pack}[/] (import a sheet with 'bankrag rules import <file.xlsx>')")
        raise typer.Exit()
    t = Table(title=f"{p.name} ({len(p.rules)} rules)")
    for c in ("clause", "id", "products", "check", "applies", "enforce", "status"):
        t.add_column(c, overflow="fold")
    for r in p.rules:
        style = "" if r.status == "active" else "dim"
        applies = "any mention" if r.trigger == "mention" else "offer / recommend"
        t.add_row(r.clause, r.id, ", ".join(r.products), r.check, applies, r.enforcement, r.status, style=style)
    console.print(t)
    for prod in p.products:
        rprint(f"[cyan]{prod.id}[/] {prod.name} -> skills: {', '.join(prod.skills) or '[yellow]none: no agent carries these rules[/]'}")


@rules_app.command("validate")
def rules_validate(pack: str = typer.Option("mccs")):
    """Check the pack the way the app loads it (products, regexes, required fields)."""
    from . import rules as RL

    s = _settings()
    p = RL.active_pack(s, pack)
    bad = 0
    for rid, (errors, warnings) in RL.validate_pack(p).items():
        bad += len(errors)
        for e in errors:
            rprint(f"[red]{rid}: {e}[/]")
        for w in warnings:
            rprint(f"[yellow]{rid}: {w}[/]")
        if not errors and not warnings:
            rprint(f"[green]{rid}: ok[/]")
    raise typer.Exit(code=1 if bad else 0)


@rules_app.command("import")
def rules_import(file: Path = typer.Argument(..., help="the compliance team's .xlsx"),
                 pack: str = typer.Option("mccs"),
                 dry_run: bool = typer.Option(False, "--dry-run", help="report what would change without writing")):
    """Write / update rules/<pack>/ from the sheet. How each rule is checked is preserved."""
    from .rules_xlsx import import_xlsx

    s = _settings()
    r = import_xlsx(s, file.read_bytes(), pack, dry_run=dry_run)
    rprint(f"{r['rows']} rows -> [green]{len(r['created'])} created[/], [yellow]{len(r['updated'])} updated[/], {len(r['unchanged'])} unchanged"
           + (" [dim](dry run: nothing written)[/]" if dry_run else ""))
    for rid in r["created"] + r["updated"]:
        rprint(f"  {rid}")
    for w in r["warnings"]:
        rprint(f"[yellow]! {w}[/]")


@rules_app.command("export")
def rules_export(out: Path = typer.Argument(Path("rules-export.xlsx")), pack: str = typer.Option("mccs")):
    """Write the pack back to a sheet (same columns, plus the rule id and how it is checked)."""
    from .rules_xlsx import export_xlsx

    out.write_bytes(export_xlsx(_settings(), pack))
    rprint(f"[green]wrote {out}[/]")


@rules_app.command("check")
def rules_check(text: str = typer.Argument(..., help="an answer to check"),
                skill: str = typer.Option("", help="the skill that produced it (helps product detection)"),
                language: str = typer.Option("th"), pack: str = typer.Option("mccs")):
    """Run the answer-time guard over a piece of text: what it would be flagged for, and what gets appended."""
    from . import rules as RL

    s = _settings()
    fixed, report = RL.guard(s, text, language=language, skill_id=skill, pack_id=pack)
    if not report:
        rprint("[yellow]no rules loaded[/]")
        raise typer.Exit()
    rprint(f"products: {', '.join(report.get('product_names') or []) or '[dim]none detected: no rule applies[/]'}")
    colour = {"compliant": "green", "non_compliant": "red", "undefined": "yellow", "not_applicable": "dim"}
    for f in report.get("findings", []):
        rprint(f"[{colour.get(f['verdict'], '')}]{f['verdict']:<15}[/] {f['clause']} {f['rule_id']}"
               + (f" [dim]- {f['detail']}[/]" if f["detail"] else "") + (" [green](fixed)[/]" if f["fixed"] else ""))
    if fixed != text:
        rprint(f"\n[green]answer after the guard:[/]\n{fixed}")


@rules_app.command("prompt")
def rules_prompt(skill: str = typer.Option("", help="skill id; empty prints the concierge block"), pack: str = typer.Option("mccs")):
    """Print the block that is compiled into an agent's instructions."""
    from . import rules as RL

    s = _settings()
    p = RL.active_pack(s, pack)
    if not skill:
        print(RL.prompt_block_for_concierge(p))
        raise typer.Exit()
    skills, _ = _skills(s)
    if skill not in skills:
        rprint(f"[red]unknown skill '{skill}'[/]")
        raise typer.Exit(code=1)
    print(RL.prompt_block_for_skill(p, skills[skill]) or "(no rules cover this skill's products)")


def main() -> None:
    try:
        app()
    except ConfigError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(code=2)


if __name__ == "__main__":
    main()
