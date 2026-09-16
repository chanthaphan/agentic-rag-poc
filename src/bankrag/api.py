"""FastAPI backend: mobile customer app (/), tester Studio (/studio, basic auth), legacy debug page (/legacy)."""
from __future__ import annotations

import secrets
import hashlib
import re
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import json
import os

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import sessions as SESS
from .config import Settings
from .models import Answer, SessionRecord
from .skills import (create_skill, delete_skill, install_skill_zip, lint_skills, load_base, load_skills, read_base, skill_to_zip,
                     validate_skill, write_base, write_skill)

import logging as _logging

# Audit lines go to a plain handler of their own: uvicorn's rich handler wraps and truncates at the console width, and
# a truncated audit line is worse than none - the part that gets cut is exactly the arguments and the outcome.
_audit_handler = _logging.StreamHandler()
_audit_handler.setFormatter(_logging.Formatter("%(asctime)s AUDIT %(name)s %(message)s"))
for _n in ("bankrag.services", "bankrag.audit"):
    _lg = _logging.getLogger(_n)
    _lg.setLevel(_logging.INFO)
    _lg.handlers = [_audit_handler]
    _lg.propagate = False  # do not also hand it to the rich handler that truncates
_audit = _logging.getLogger("bankrag.audit")

app = FastAPI(title="bankrag POC", version="0.2.0")
settings = Settings.load()
_backup_dest = Path(os.environ["SQLITE_DB_BACKUP"]) if os.environ.get("SQLITE_DB_BACKUP") else None
if _backup_dest is not None:
    if SESS.restore_db(settings, _backup_dest):
        print(f"restored session database from {_backup_dest}")
    with SESS.connect(settings):  # create the DB/schema now so the first backup happens even before any chat
        pass
    SESS.start_backup_thread(settings, _backup_dest, int(os.environ.get("SQLITE_DB_BACKUP_INTERVAL", "60")))
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
_sessions: "OrderedDict[str, object]" = OrderedDict()  # live ChatSession cache (LRU)
_MAX_LIVE = 50
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
security = HTTPBasic(auto_error=False)


def _skills():
    return load_skills(settings.skills_dir), load_base(settings.skills_dir)


# ---------------- auth (Studio) ----------------
STUDIO_COOKIE = "bankrag_studio"
TESTER_COOKIE = "bankrag_tester"


def _pw_ok(value: Optional[str]) -> bool:
    pw = settings.studio_password
    return bool(pw and value) and secrets.compare_digest(value.encode(), pw.encode())


def _identity_from_headers(request: Request) -> dict:
    return identity(request)  # defined below (Easy Auth headers); kept behind a function so auth helpers can sit up here


STAFF_ROLES = ("admin", "tester")  # the Studio workbench; "external" only gets the chat page


def studio_role(request: Request, creds: Optional[HTTPBasicCredentials]) -> Optional[str]:
    """Who is on the access list, and as what (admin / tester / external).
    - Signed in through Entra (Easy Auth headers): the access list decides. The shared password is not accepted for
      SSO users once at least one person is on the list, so access is managed purely by identity.
    - No SSO identity (local dev, curl with basic auth): the STUDIO_PASSWORD grants admin, as before."""
    who = _identity_from_headers(request)
    if who["email"]:
        role = SESS.get_role(settings, who["email"])
        if role:
            return role
        if SESS.access_count(settings) > 0:
            return None
    pw_ok = (creds is not None and _pw_ok(creds.password)) or _pw_ok(request.cookies.get(STUDIO_COOKIE))
    return "admin" if pw_ok else None


def _studio_authed(request: Request, creds: Optional[HTTPBasicCredentials]) -> bool:
    return studio_role(request, creds) is not None


def _is_staff(request: Request) -> bool:
    """A Studio member (admin or tester). External people are on the access list but are not staff."""
    return studio_role(request, None) in STAFF_ROLES


def require_studio(request: Request, creds: Optional[HTTPBasicCredentials] = Depends(security)) -> str:
    """Studio access: Entra identity on the access list as admin or tester, or (without SSO) HTTP basic auth / the
    password cookie. The external role is refused here: it has the chat page and the public chat routes only."""
    role = studio_role(request, creds)
    if role in STAFF_ROLES:
        return role
    if role == "external":
        raise HTTPException(403, "your account has the external role: the chat page only, not the Studio tools")
    if _identity_from_headers(request)["email"]:
        raise HTTPException(403, "your account is not on the Studio access list; ask a Studio admin to add you")
    if not settings.studio_password:
        raise HTTPException(503, "Studio is disabled: set STUDIO_PASSWORD in .env or STUDIO_ADMINS")
    raise HTTPException(401, "Studio password required", headers={"WWW-Authenticate": 'Basic realm="bankrag studio"'})


def require_admin(role: str = Depends(require_studio)) -> str:
    if role != "admin":
        raise HTTPException(403, "Studio admins only")
    return role


studio = APIRouter(dependencies=[Depends(require_studio)])


# ---------------- models ----------------
class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    force_skill: Optional[str] = None
    with_sources: bool = True
    source: str = "app"  # app | studio
    lat: Optional[float] = None  # the customer's position, sent only when they allow it for a "near me" question
    lon: Optional[float] = None


class ChatResponse(BaseModel):
    session_id: str
    title: str
    answer: Answer


class SkillForm(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    product_category: Optional[str] = None
    keywords: Optional[list[str] | str] = None
    model: Optional[str] = None
    top_k: Optional[int] = None
    filter: Optional[str] = None
    suggestions: Optional[list[str] | str] = None
    body: Optional[str] = None


# ---------------- jobs ----------------
def _start_job(kind: str, fn, meta: Optional[dict] = None) -> str:
    """Run fn(log) on a thread. `log(msg)` appends to the job log; `log.progress(phase=, done=, total=, message=, **stats)`
    updates the structured progress that the Studio renders (phase stepper, bar, counters)."""
    job_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    _jobs[job_id] = {"id": job_id, "kind": kind, "meta": meta or {}, "status": "running", "log": [], "started": now.isoformat(), "finished": None,
                     "elapsed_ms": 0, "progress": {"phase": "queued", "done": None, "total": None, "message": "", "stats": {}}, "result": None}
    for old_id in list(_jobs)[:-50]:  # keep the 50 most recent jobs in memory
        if _jobs[old_id]["status"] != "running":
            _jobs.pop(old_id, None)

    def log(msg: str) -> None:
        _jobs[job_id]["log"].append(msg)
        _jobs[job_id]["elapsed_ms"] = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)

    def progress(phase: Optional[str] = None, done: Optional[int] = None, total: Optional[int] = None, message: Optional[str] = None, **stats) -> None:
        pr = _jobs[job_id]["progress"]
        if phase:
            pr["phase"] = phase
        pr["done"], pr["total"] = done, total
        if message is not None:
            pr["message"] = message
        pr["stats"].update({k: v for k, v in stats.items() if v is not None})
        _jobs[job_id]["elapsed_ms"] = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)

    log.progress = progress  # type: ignore[attr-defined]

    def run() -> None:
        try:
            _jobs[job_id]["result"] = fn(log)
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["progress"]["phase"] = "done"
        except Exception as e:  # noqa: BLE001
            log(f"ERROR {type(e).__name__}: {e}")
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["progress"]["phase"] = "error"
            _jobs[job_id]["progress"]["message"] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            _jobs[job_id]["finished"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["elapsed_ms"] = int((datetime.now(timezone.utc) - now).total_seconds() * 1000)

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] == "running":
        job["elapsed_ms"] = int((datetime.now(timezone.utc) - datetime.fromisoformat(job["started"])).total_seconds() * 1000)
    return job


@app.get("/jobs")
def list_jobs(kind: Optional[str] = None, limit: int = 10):
    """Recent jobs, newest first, without their logs. kind = comma-separated filter (e.g. ingest,crawl)."""
    kinds = {k.strip() for k in (kind or "").split(",") if k.strip()}
    rows = [j for j in reversed(list(_jobs.values())) if not kinds or j["kind"] in kinds]
    return [{k: v for k, v in j.items() if k != "log"} for j in rows[: max(1, min(limit, 50))]]


# ---------------- health / app config ----------------
@app.get("/health")
def health():
    return {
        "ok": True,
        "project_endpoint": settings.project_endpoint,
        "search_endpoint": settings.search_endpoint,
        "index": settings.search_index,
        "kb_reasoning_effort": settings.kb_reasoning_effort,
        "kb_mcp_auth": settings.kb_mcp_auth,
        "studio_enabled": bool(settings.studio_password),
        "configured": bool(settings.project_endpoint and settings.search_endpoint and settings.search_admin_key),
    }


@app.get("/app/config")
def app_config(request: Request):
    from .chat import suggestion_language

    who = identity(request)

    skills, _ = _skills()
    ordered = ([skills["general"]] if "general" in skills else []) + [s for k, s in skills.items() if k != "general"]
    by_lang: dict[str, list[str]] = {"th": [], "en": []}
    for sk in ordered:
        for x in sk.suggestions:
            lang = suggestion_language(x)
            if x not in by_lang[lang] and len(by_lang[lang]) < 3:
                by_lang[lang].append(x)
    starters = by_lang["th"][:3] if by_lang["th"] else by_lang["en"][:3]
    return {
        "starter_prompts_by_lang": by_lang,
        "user_name": (who["name"].split()[0] if who["name"] else "") or settings.app_user_name,
        "user_initials": _initials(who["name"]) or settings.app_user_initials,
        "user_email": who["email"],
        "assistant_name": settings.assistant_name,
        "maps_key": settings.google_maps_key,  # empty: a place card links out to Maps instead of embedding it
        "starter_prompts": starters[:3],
        "skills": [{"id": s.id, "name": s.name, "product_category": s.product_category} for s in skills.values()],
    }


# ---------------- identity (Easy Auth / SSO) ----------------
def identity(request: Request) -> dict:
    """Who is signed in, from Azure Container Apps Easy Auth headers. Empty when running without SSO (local dev)."""
    name, email = "", ""
    raw = request.headers.get("x-ms-client-principal", "")
    if raw:
        try:
            import base64

            claims = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")).get("claims") or []
            by_type = {}
            for c in claims:
                by_type.setdefault(str(c.get("typ", "")).lower(), str(c.get("val", "")))
            name = by_type.get("name") or by_type.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name", "")
            email = (by_type.get("preferred_username") or by_type.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
                     or by_type.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn") or by_type.get("email") or "")
        except Exception:  # noqa: BLE001
            pass
    if not email:
        email = request.headers.get("x-ms-client-principal-name", "")
    if not name and email:
        name = email.split("@")[0].replace(".", " ").title()
    return {"name": name[:80], "email": email[:120]}


def _initials(name: str) -> str:
    parts = [p for p in name.replace("@", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() if parts else ""


@app.get("/whoami")
def whoami(request: Request):
    who = identity(request)
    return {**who, "tester": request.cookies.get(TESTER_COOKIE, ""), "sso": bool(who["email"])}


# ---------------- sessions + chat ----------------
def _get_session(sid: Optional[str], skills):
    """Return (session_id, ChatSession, SessionRecord); rehydrates from disk or creates a new one."""
    from .chat import ChatSession

    if sid and sid in _sessions:
        _sessions.move_to_end(sid)
        rec = SESS.load_session(settings, sid) or SESS.new_record(sid)
        cs = _sessions[sid]
        cs.skills = skills
        return sid, cs, rec
    rec = SESS.load_session(settings, sid) if sid else None
    if rec is None:
        rec = SESS.new_record(sid if sid and SESS.ID_RE.match(sid) else None)
        cs = ChatSession(settings, skills)
    else:
        cs = ChatSession.from_record(settings, skills, rec)
    _sessions[rec.id] = cs
    while len(_sessions) > _MAX_LIVE:
        _sessions.popitem(last=False)
    return rec.id, cs, rec


def _stamp_owner(rec, request: Request) -> str:
    """Record who is chatting (SSO name/email) on a new session; returns the display name for the turn."""
    who = identity(request)
    if not rec.user_name and (who["name"] or who["email"]):
        rec.user_name, rec.user_email = who["name"], who["email"]
    return who["name"] or rec.user_name


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    skills, _ = _skills()
    if req.session_id and not SESS.ID_RE.match(req.session_id):
        raise HTTPException(400, "bad session id")
    with _lock:
        sid, session, rec = _get_session(req.session_id, skills)
        _require_owner(request, rec)
        by = _stamp_owner(rec, request)
    try:
        ans = session.ask(req.message, force_skill=req.force_skill, with_sources=req.with_sources)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:500]}") from e
    with _lock:
        SESS.append_turns(rec, req.message, ans, by=by)
        session.to_record(rec)
        SESS.save_session(settings, rec)
    return ChatResponse(session_id=sid, title=rec.title, answer=ans)


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, request: Request):
    """Server-sent events: route, delta*, tool*, done(answer) | error. Persists the session on done."""
    skills, _ = _skills()
    if req.session_id and not SESS.ID_RE.match(req.session_id):
        raise HTTPException(400, "bad session id")
    with _lock:
        sid, session, rec = _get_session(req.session_id, skills)
        _require_owner(request, rec)
        by = _stamp_owner(rec, request)
    if not rec.turns and req.source:
        rec.source = req.source

    def gen():
        yield f"data: {json.dumps({'type': 'session', 'session_id': sid}, ensure_ascii=False)}\n\n"
        try:
            loc = (req.lat, req.lon) if req.lat is not None and req.lon is not None else None
            _audit.info("chat session=%s source=%s location=%s q=%r", (sid or "")[:8], req.source,
                        "yes" if loc else "no", req.message[:80])
            for ev in session.ask_stream(req.message, force_skill=req.force_skill, with_sources=req.with_sources,
                                              location=loc):
                if ev["type"] == "done":
                    ans: Answer = ev["answer"]
                    with _lock:
                        SESS.append_turns(rec, req.message, ans, by=by)
                        session.to_record(rec)
                        SESS.save_session(settings, rec)
                    yield f"data: {json.dumps({'type': 'done', 'session_id': sid, 'title': rec.title, 'answer': ans.model_dump()}, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': f'{type(e).__name__}: {str(e)[:400]}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _owns(request: Request, rec) -> bool:
    """Signed-in people see only their own sessions in the app; Studio members (admin or tester) may open any session.
    External people are treated like app users: their own sessions only. Without SSO (local dev) everything is visible."""
    who = identity(request)
    if not who["email"]:
        return True
    if not rec.user_email and not rec.user_name:
        return True  # a conversation nobody has claimed yet (its first message): whoever is sending it becomes the owner
    if rec.user_email and rec.user_email.lower() == who["email"].lower():
        return True
    if not rec.user_email and rec.user_name and rec.user_name == who["name"]:
        return True
    return _is_staff(request)


@app.get("/sessions/export.xlsx")
def sessions_export_xlsx(request: Request):
    """The signed-in person's own chat log as a workbook (questions, answers, ratings, dislike reasons and comments);
    offered on the external page. Without SSO (local dev) it holds everything, like the rest of the app."""
    from .eval_report import chatlog_workbook

    who = identity(request)
    rows = SESS.question_rows(settings, owner_email=who["email"], owner_name=who["name"], limit=5000) if who["email"] else SESS.question_rows(settings, limit=5000)
    return Response(chatlog_workbook(rows, who["name"] or who["email"]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="my-chat-log.xlsx"', "Cache-Control": "no-store"})


def _require_owner(request: Request, rec) -> None:
    if not _owns(request, rec):
        raise HTTPException(403, "this conversation belongs to someone else")


@app.get("/sessions")
def list_sessions(request: Request, all: bool = False):
    """The app's history list: only the signed-in person's sessions. Studio members may pass all=1 to list everything."""
    who = identity(request)
    if not who["email"] or (all and _is_staff(request)):
        return SESS.list_sessions(settings)
    return SESS.list_sessions(settings, owner_email=who["email"], owner_name=who["name"])


@app.get("/sessions/stats")
def sessions_stats():
    return SESS.stats(settings)


@app.get("/app/pricing")
def get_pricing():
    from .pricing import load_pricing

    return load_pricing(settings)


@studio.put("/app/pricing", dependencies=[Depends(require_admin)])
def put_pricing(data: dict):
    from .pricing import save_pricing

    return save_pricing(settings, data)


@app.get("/sessions/review", dependencies=[Depends(require_studio)])
def sessions_review(skill: str = "", rating: str = "", user: str = ""):
    return SESS.review_list(settings, skill=skill, rating=rating, user=user)


@studio.get("/conversations/users")
def conversation_users():
    return SESS.known_users(settings)


@studio.get("/conversations/questions")
def conversation_questions(skill: str = "", rating: str = "", q: str = "", source: str = "", user: str = "", comment: str = "", limit: int = 300):
    return SESS.question_rows(settings, skill=skill, rating=rating, q=q[:200], source=source, user=user, comment=comment[:100], limit=limit)


class QuestionItems(BaseModel):
    items: list[dict]  # [{session_id, idx}]


def _question_items(req: QuestionItems) -> list[tuple[str, int]]:
    items = [(str(i.get("session_id", "")), int(i.get("idx", -1))) for i in req.items[:2000]]
    items = [(s, i) for s, i in items if SESS.ID_RE.match(s) and i >= 0]
    if not items:
        raise HTTPException(400, "no questions selected")
    return items


@studio.post("/conversations/export.xlsx")
def conversation_questions_xlsx(req: QuestionItems):
    from .eval_report import questions_workbook

    items = _question_items(req)
    rows = SESS.question_rows(settings, items=items, limit=len(items))
    order = {k: n for n, k in enumerate(items)}
    rows.sort(key=lambda r: order.get((r["session_id"], r["idx"]), 1e9))
    return Response(questions_workbook(rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="bankrag-questions.xlsx"'})


class AppendCases(BaseModel):
    cases: list[dict]


@studio.post("/evals/{set_name}/append")
def append_eval_cases(set_name: str, req: AppendCases):
    from .evals import append_cases

    try:
        return append_cases(settings, set_name, req.cases[:500])
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


def reconcile_pending(hours: int = 24, limit: int = 50) -> dict:
    """Read specialist usage from Application Insights for recent handoff turns still marked pending."""
    from . import observability as OBS

    if not OBS.enabled(settings):
        return {"enabled": False, "checked": 0, "updated": 0}
    checked = updated = 0
    for sid, idx in SESS.pending_handoff_turns(settings, hours=hours, limit=limit):
        rec = SESS.load_session(settings, sid)
        if rec is None or idx >= len(rec.turns):
            continue
        checked += 1
        trace = dict(rec.turns[idx].trace or {})
        try:
            if OBS.reconcile_trace(settings, trace):
                SESS.update_turn_trace(settings, sid, idx, trace)
                updated += 1
            elif int((trace.get("handoff") or {}).get("reconcile_attempts", 0)) >= 20:
                trace.setdefault("handoff", {})["usage_pending"] = False  # give up after ~30 min of polling
                trace["handoff"]["reconcile_note"] = "specialist spans never appeared in Application Insights"
                SESS.update_turn_trace(settings, sid, idx, trace)
            else:
                SESS.update_turn_trace(settings, sid, idx, trace)  # persists the attempt counter
        except Exception as e:  # noqa: BLE001
            log_msg = f"reconcile {sid}#{idx}: {type(e).__name__}: {str(e)[:120]}"
            print(log_msg)
            break
    return {"enabled": True, "checked": checked, "updated": updated}


def _start_reconcile_thread() -> None:
    from . import observability as OBS

    if not OBS.enabled(settings):
        return

    def loop() -> None:
        import time as _t

        _t.sleep(60)
        while True:
            try:
                reconcile_pending()
            except Exception as e:  # noqa: BLE001
                print(f"reconcile thread: {type(e).__name__}: {e}")
            _t.sleep(90)

    threading.Thread(target=loop, daemon=True, name="handoff-reconcile").start()


@app.post("/sessions/{session_id}/reconcile")
def reconcile_session(session_id: str, request: Request):
    """Pull the specialist's tokens for this conversation's handoff answers now (otherwise a background job does it every 90 s)."""
    from . import observability as OBS

    if not SESS.ID_RE.match(session_id):
        raise HTTPException(400, "bad session id")
    rec = SESS.load_session(settings, session_id)
    if rec is None:
        raise HTTPException(404, "session not found")
    _require_owner(request, rec)
    if not OBS.enabled(settings):
        return {"enabled": False, "updated": 0, "pending": 0}
    updated = pending = 0
    for i, turn in enumerate(rec.turns):
        ho = (turn.trace or {}).get("handoff") or {}
        if ho.get("usage_pending"):
            trace = dict(turn.trace)
            try:
                ok = OBS.reconcile_trace(settings, trace)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"trace query failed: {type(e).__name__}: {str(e)[:200]}") from e
            SESS.update_turn_trace(settings, session_id, i, trace)
            updated += int(ok)
            pending += int(not ok)
    return {"enabled": True, "updated": updated, "pending": pending, "session": SESS.load_session(settings, session_id)}


@app.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request):
    if not SESS.ID_RE.match(session_id):
        raise HTTPException(400, "bad session id")
    rec = SESS.load_session(settings, session_id)
    if rec is None:
        raise HTTPException(404, "session not found")
    _require_owner(request, rec)
    return rec


@app.delete("/sessions/{session_id}", dependencies=[Depends(require_admin)])
def delete_session_endpoint(session_id: str):
    if not SESS.ID_RE.match(session_id):
        raise HTTPException(400, "bad session id")
    with _lock:
        s = _sessions.pop(session_id, None)
    if s is not None:
        s.reset()
    return {"ok": SESS.delete_session(settings, session_id)}


@app.post("/chat/{session_id}/reset")
def chat_reset(session_id: str, request: Request):
    if not SESS.ID_RE.match(session_id):
        raise HTTPException(400, "bad session id")
    rec = SESS.load_session(settings, session_id)
    if rec is not None:
        _require_owner(request, rec)
    return delete_session_endpoint(session_id)


# ---------------- base rules, settings, models ----------------
@app.get("/skills/_base")
def get_base():
    return read_base(settings.skills_dir)


@studio.put("/skills/_base", dependencies=[Depends(require_admin)])
def put_base(data: dict):
    try:
        return write_base(settings.skills_dir, str(data.get("body", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


_models_cache: dict = {"at": 0.0, "items": []}


@app.get("/app/models")
def list_models(refresh: bool = False):
    """Live model deployments of the Foundry project (cached 120 s)."""
    import time

    if not refresh and _models_cache["items"] and time.time() - _models_cache["at"] < 120:
        return _models_cache["items"]
    items = []
    try:
        from .foundry import project_client

        for d in project_client(settings).deployments.list():
            items.append({"name": getattr(d, "name", ""), "model": getattr(d, "model_name", "") or "", "publisher": getattr(d, "model_publisher", "") or "", "type": getattr(d, "type", "") or ""})
        items = [i for i in items if i["name"] and not any(x in i["name"] for x in ("embedding", "audio", "realtime", "image"))]
        _models_cache.update(at=time.time(), items=items)
    except Exception as e:  # noqa: BLE001
        if not _models_cache["items"]:
            return [{"name": settings.default_chat_model, "model": settings.default_chat_model, "publisher": "", "type": "", "error": f"{type(e).__name__}"}]
        return _models_cache["items"]
    return items


@studio.get("/app/settings")
def get_app_settings():
    from .config import OVERLAY_KEYS, load_overlay

    eff = {"ROUTER_MODEL": settings.router_model, "DEFAULT_CHAT_MODEL": settings.default_chat_model, "KB_REASONING_EFFORT": settings.kb_reasoning_effort,
           "KB_MAX_OUTPUT_TOKENS": settings.kb_max_output_tokens, "ASSISTANT_NAME": settings.assistant_name, "APP_USER_NAME": settings.app_user_name,
           "APP_USER_INITIALS": settings.app_user_initials, "KB_LLM_DEPLOYMENT": settings.kb_llm_deployment, "JUDGE_MODEL": settings.judge_model, "FOUNDRY_NATIVE_SKILLS": "1" if settings.foundry_native_skills else "0",
           "ORCHESTRATION_MODE": settings.orchestration_mode, "CONCIERGE_MODEL": settings.concierge_model,
           "SUGGESTIONS_MODE": settings.suggestions_mode, "SUGGESTIONS_MODEL": settings.suggestions_model}
    return {"keys": list(OVERLAY_KEYS), "effective": eff, "overlay": load_overlay(settings.root)}


@studio.put("/app/settings", dependencies=[Depends(require_admin)])
def put_app_settings(data: dict):
    from .config import save_overlay

    saved = save_overlay(settings.root, {k: ("" if v is None else str(v)) for k, v in data.items()})
    fresh = Settings.load(settings.root)
    settings.__dict__.update(fresh.__dict__)  # hot-reload for this process
    return {"overlay": saved, "note": "applied; run skills sync to update agents that use ROUTER_MODEL / DEFAULT_CHAT_MODEL / KB settings"}


@app.post("/route")
def route_only(data: dict):
    """Router-only check (no answer): which skill would this message go to?"""
    from . import router as R
    from .foundry import project_client

    skills, _ = _skills()
    msg = str(data.get("message", "")).strip()
    if not msg:
        raise HTTPException(400, "message required")
    d = R.route(project_client(settings).get_openai_client(), msg, [], data.get("prev_skill"), skills)
    return d.model_dump()


@app.get("/skills/lint")
def skills_lint():
    skills, _ = _skills()
    models = [m["name"] for m in list_models()] or None
    from .foundry_sync import plan_kb_owners

    owners = plan_kb_owners(settings, skills)
    has_docs = {sid: owners[sid].id == sid or skills[sid].product_category in ("all", "*", "") for sid in skills}
    return lint_skills(skills, settings.knowledge_dir, models, has_docs)


@app.get("/skills/{skill_id}/versions")
def skill_versions(skill_id: str):
    from .foundry import project_client

    skills, _ = _skills()
    spec = skills.get(skill_id)
    if not spec:
        raise HTTPException(404, "skill not found")
    out = []
    try:
        for v in project_client(settings).agents.list_versions(agent_name=spec.agent_name):
            d = v.definition.as_dict() if getattr(v, "definition", None) else {}
            out.append({"version": str(v.version), "created_at": getattr(v, "created_at", None), "model": d.get("model", ""), "metadata": dict(v.metadata or {}), "description": getattr(v, "description", "") or "", "tools": [t.get("type") for t in d.get("tools", [])]})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"{type(e).__name__}: {str(e)[:300]}") from e
    out.sort(key=lambda r: int(r["version"]) if r["version"].isdigit() else 0, reverse=True)
    return out


@app.get("/skills/{skill_id}/versions/{version}")
def skill_version(skill_id: str, version: str):
    from .foundry import project_client
    from .foundry_sync import desired_definition, plan_kb_owners

    skills, base = _skills()
    spec = skills.get(skill_id)
    if not spec:
        raise HTTPException(404, "skill not found")
    try:
        v = project_client(settings).agents.get_version(agent_name=spec.agent_name, agent_version=version)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"version not found: {type(e).__name__}") from e
    d = v.definition.as_dict()
    local = desired_definition(settings, spec, base, plan_kb_owners(settings, skills)[skill_id]).as_dict()
    return {"version": str(v.version), "model": d.get("model"), "instructions": d.get("instructions", ""), "tools": d.get("tools", []), "metadata": dict(v.metadata or {}),
            "local_instructions": local.get("instructions", ""), "local_model": local.get("model")}


# ---------------- skills (read: open; write: studio) ----------------
@app.get("/skills")
def list_skills(remote: bool = True):
    from .foundry_sync import status

    skills, base = _skills()
    if remote:
        return status(settings, skills, base)
    return [{"id": s.id, "name": s.name, "product_category": s.product_category, "model": s.model or settings.default_chat_model, "state": "-"} for s in skills.values()]


@app.get("/skills/{skill_id}")
def get_skill(skill_id: str):
    import frontmatter

    skills, base = _skills()
    spec = skills.get(skill_id)
    if not spec:
        raise HTTPException(404, "skill not found")
    errors, warnings = validate_skill(spec, settings.knowledge_dir)
    post = frontmatter.load(spec.path / "SKILL.md")
    return {"spec": spec.model_dump(exclude={"path"}), "frontmatter": dict(post.metadata), "body": post.content.strip(),
            "skill_md": (spec.path / "SKILL.md").read_text(encoding="utf-8"), "errors": errors, "warnings": warnings}


@studio.put("/skills/{skill_id}")
def update_skill(skill_id: str, form: SkillForm):
    try:
        spec = write_skill(settings.skills_dir, skill_id, form.model_dump(exclude_none=True, exclude={"body", "id"}), form.body or "")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    errors, warnings = validate_skill(spec, settings.knowledge_dir)
    return {"spec": spec.model_dump(exclude={"path"}), "errors": errors, "warnings": warnings}


@studio.post("/skills")
def create_skill_endpoint(form: SkillForm):
    try:
        spec = create_skill(settings.skills_dir, form.model_dump(exclude_none=True))
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    errors, warnings = validate_skill(spec, settings.knowledge_dir)
    return {"spec": spec.model_dump(exclude={"path"}), "errors": errors, "warnings": warnings}


@studio.delete("/skills/{skill_id}", dependencies=[Depends(require_admin)])
def delete_skill_endpoint(skill_id: str, prune: bool = True):
    try:
        delete_skill(settings.skills_dir, skill_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    job_id = None
    if prune:
        from .foundry_sync import sync_skills

        def run(log):
            skills, base = _skills()
            return sync_skills(settings, skills, base, prune=True, log=log).model_dump()

        job_id = _start_job("prune", run)
    return {"ok": True, "job_id": job_id}


@studio.get("/skills/{skill_id}/zip")
def download_skill(skill_id: str):
    skills, _ = _skills()
    spec = skills.get(skill_id)
    if not spec:
        raise HTTPException(404, "skill not found")
    return Response(skill_to_zip(spec), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{skill_id}.zip"'})


@studio.post("/skills/upload")
async def upload_skill(file: UploadFile = File(...)):
    data = await file.read()
    try:
        spec = install_skill_zip(data, settings.skills_dir)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"invalid skill zip: {e}") from e
    errors, warnings = validate_skill(spec, settings.knowledge_dir)
    return {"id": spec.id, "name": spec.name, "errors": errors, "warnings": warnings}


@studio.post("/skills/sync")
def sync_skills_endpoint(only: Optional[str] = None, prune: bool = False, register_native: bool = False, role: str = Depends(require_studio)):
    from .foundry_sync import sync_skills

    if prune and role != "admin":
        raise HTTPException(403, "prune is for Studio admins")

    def run(log):
        skills, base = _skills()
        return sync_skills(settings, skills, base, only=only, prune=prune, register_native=register_native, log=log).model_dump()

    return {"job_id": _start_job("sync", run)}


# ---------------- Foundry skill registry ----------------
@studio.get("/registry/skills")
def registry_skills():
    from .foundry import project_client
    from .foundry_native import list_registry

    skills, _ = _skills()
    try:
        with project_client(settings) as pc:
            return list_registry(pc, set(skills))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}") from e


@studio.get("/registry/skills/{name}")
def registry_skill_content(name: str, version: Optional[str] = None):
    from .foundry import project_client
    from .foundry_native import download_skill_md

    try:
        with project_client(settings) as pc:
            meta, body = download_skill_md(pc, name, version)
        return {"name": name, "frontmatter": meta, "body": body}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}") from e


class RegistryImport(BaseModel):
    name: str
    version: Optional[str] = None
    id: Optional[str] = None


@studio.post("/registry/import")
def registry_import(req: RegistryImport):
    from .foundry import project_client
    from .foundry_native import import_registry_skill

    try:
        with project_client(settings) as pc:
            spec = import_registry_skill(pc, settings, req.name, req.version, req.id)
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}") from e
    return {"id": spec.id, "name": spec.name, "note": "imported as a local skill; review keywords and description, then Save & sync"}


# ---------------- responsible lending rules ----------------
@app.get("/rules")
def list_rules(pack: str = "mccs"):
    """The rule pack as Studio shows it: products (with the skills that carry them) and one row per rule."""
    from . import rules as RL

    try:
        p = RL.active_pack(settings, pack)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    skills, _ = _skills()
    checks = RL.validate_pack(p)
    return {
        "pack": {"id": p.id, "name": p.name, "description": p.description, "sources": p.sources, "body": p.body},
        "packs": RL.list_packs(settings),
        "skills": sorted(skills),
        "pack_errors": checks.get("(pack)", ([], []))[0],  # e.g. duplicate product ids: not attached to any rule row
        "products": [{**x.model_dump(), "rules": [r.id for r in p.rules if x.id in r.products],
                      "unknown_skills": [s for s in x.skills if s not in skills]} for x in p.products],
        "rules": [{**r.model_dump(exclude={"path"}),
                   "product_names": [p.product(x).name if p.product(x) else x for x in r.products],
                   "skills": sorted({s for x in r.products for s in (p.product(x).skills if p.product(x) else [])}),
                   "errors": checks.get(r.id, ([], []))[0], "warnings": checks.get(r.id, ([], []))[1]} for r in p.rules],
    }


@app.get("/rules/prompt")
def rules_prompt(pack: str = "mccs", skill: str = ""):
    """The compiled block that goes into an agent's instructions (empty skill = the concierge's)."""
    from . import rules as RL

    try:
        p = RL.active_pack(settings, pack)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not skill:
        return {"target": "concierge", "block": RL.prompt_block_for_concierge(p)}
    skills, _ = _skills()
    if skill not in skills:
        raise HTTPException(404, "skill not found")
    return {"target": skill, "block": RL.prompt_block_for_skill(p, skills[skill])}


MAX_CHECK_CHARS = 20000  # a chat answer is a few thousand characters; the guard runs every rule regex over this


@studio.post("/rules/check")
def rules_check(data: dict):
    """Run the answer-time guard over any text: what a turn would be flagged for, and what would be appended."""
    from . import rules as RL

    text = str(data.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "text required")
    if len(text) > MAX_CHECK_CHARS:
        raise HTTPException(413, f"text is {len(text)} characters; the check accepts up to {MAX_CHECK_CHARS}")
    try:
        fixed, report = RL.guard(settings, text, question=str(data.get("question", ""))[:MAX_CHECK_CHARS], language=str(data.get("language", "th")),
                                 skill_id=str(data.get("skill", "")), pack_id=str(data.get("pack", "mccs")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"text": fixed, "report": report}


@studio.post("/rules", dependencies=[Depends(require_admin)])
def create_rule_endpoint(form: dict):
    from . import rules as RL

    try:
        r = RL.create_rule(settings, str(form.get("pack", "mccs")), form)
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return r.model_dump(exclude={"path"})


@studio.delete("/rules/{rule_id}", dependencies=[Depends(require_admin)])
def delete_rule_endpoint(rule_id: str, pack: str = "mccs"):
    from . import rules as RL

    try:
        RL.delete_rule(settings, pack, rule_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "deleted": rule_id}


@studio.put("/rules/products/{product_id}", dependencies=[Depends(require_admin)])
def upsert_product_endpoint(product_id: str, form: dict):
    """Which skill agents carry a product family's rules, plus how the family is detected in a question or answer."""
    from . import rules as RL

    try:
        p = RL.upsert_product(settings, str(form.get("pack", "mccs")), product_id, form)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return p.model_dump()


@studio.delete("/rules/products/{product_id}", dependencies=[Depends(require_admin)])
def delete_product_endpoint(product_id: str, pack: str = "mccs"):
    from . import rules as RL

    try:
        RL.delete_product(settings, pack, product_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "deleted": product_id}


@studio.put("/rules/{rule_id}", dependencies=[Depends(require_admin)])
def update_rule_endpoint(rule_id: str, form: dict):
    from . import rules as RL

    try:
        r = RL.update_rule(settings, str(form.get("pack", "mccs")), rule_id, form)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return r.model_dump(exclude={"path"})


@studio.get("/rules.xlsx")
def rules_xlsx_export(pack: str = "mccs"):
    """The whole rule set as a sheet: Studio-only, like /bundle.zip."""
    from . import rules as RL
    from .rules_xlsx import export_xlsx

    try:
        RL.pack_dir(settings, pack)  # validated before it reaches a path or the Content-Disposition header
        data = export_xlsx(settings, pack)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{pack}-rules.xlsx"'})


@studio.post("/rules/import", dependencies=[Depends(require_admin)])
async def rules_import(pack: str = "mccs", dry_run: bool = False, file: UploadFile = File(...)):
    from .rules_xlsx import import_xlsx

    try:
        return import_xlsx(settings, await file.read(), pack, dry_run=dry_run)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------------- knowledge ----------------
@app.get("/knowledge/stats")
def knowledge_stats():
    from . import search_index as SI
    from .ingest.pipeline import local_counts

    local = local_counts(settings)
    facets, stats, err = {}, {}, None
    try:
        facets = SI.facet_counts(settings)
        stats = SI.index_stats(settings)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:200]}"
    cats = sorted(set(local) | set(facets) | {s.product_category for s in _skills()[0].values() if s.product_category not in ("all", "*", "")})
    rows = [{"category": c, "files": local.get(c, {}).get("files", 0), "manifest_chunks": local.get(c, {}).get("chunks", 0), "indexed_chunks": facets.get(c, 0)} for c in cats]
    return {"rows": rows, "index": stats, "error": err}


@studio.get("/knowledge/files")
def knowledge_files(category: str):
    from .ingest.pipeline import list_knowledge_files

    return list_knowledge_files(settings, category)


@studio.delete("/knowledge/files", dependencies=[Depends(require_admin)])
def delete_knowledge_file_endpoint(path: str):
    from .ingest.pipeline import delete_knowledge_file

    try:
        delete_knowledge_file(settings, path)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "note": "run ingest to remove its chunks from the index"}


@studio.post("/knowledge/upload")
async def upload_knowledge(category: str, files: list[UploadFile] = File(...)):
    if not category or "/" in category or category.startswith((".", "_")):
        raise HTTPException(400, "bad category")
    target = settings.knowledge_dir / category / "_uploads"
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = Path(f.filename or "upload").name
        if Path(name).suffix.lower() not in (".md", ".pdf", ".txt"):
            raise HTTPException(400, f"unsupported file type: {name}")
        if name.lower().endswith(".txt"):
            name = name[:-4] + ".md"
        (target / name).write_bytes(await f.read())
        saved.append(str((target / name).relative_to(settings.knowledge_dir)))
    return {"saved": saved}


@studio.post("/knowledge/ingest")
def ingest_endpoint(category: Optional[str] = None, full: bool = False, role: str = Depends(require_studio)):
    from .ingest.pipeline import ingest

    if full and role != "admin":
        raise HTTPException(403, "full re-ingest is for Studio admins (it re-embeds every file); run the normal ingest instead")

    def run(log):
        rep = ingest(settings, category=category, full=full, log=log)
        return {"summary": rep.summary(), "uploaded": rep.uploaded_chunks, "deleted": rep.deleted_chunks, "per_category": rep.per_category}

    return {"job_id": _start_job("ingest", run, {"category": category or "all", "full": full})}


@app.get("/knowledge/retrieve")
def retrieve_endpoint(q: str, skill: str = "credit-card", max_docs: int = 5):
    from . import knowledge_base as KB
    from .foundry_sync import synced_kb_owners

    skills, _ = _skills()
    if skill not in skills:
        raise HTTPException(404, "skill not found")
    spec = synced_kb_owners(settings, skills)[skill]
    try:
        return [r.model_dump() for r in KB.retrieve(settings, spec.kb_name, q, ks_name=spec.ks_name, max_docs=max_docs)]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:500]}") from e


# ---------------- knowledge: chunks, search, reingest, import, bundle ----------------
@studio.get("/knowledge/chunks")
def knowledge_chunks(path: str):
    from . import search_index as SI
    from .chat import _tokens

    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "bad path")
    try:
        rows = SI.chunks_for_doc(settings, path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}") from e
    return [{"id": r.get("id"), "chunk_index": r.get("chunk_index"), "breadcrumb": r.get("breadcrumb", ""), "content": r.get("content", ""), "tokens": _tokens(r.get("content", ""))} for r in rows]


@studio.get("/knowledge/search")
def knowledge_search(q: str, category: Optional[str] = None, k: int = 5):
    from . import search_index as SI

    try:
        return SI.hybrid_search(settings, q, category=category or None, k=max(1, min(k, 20)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}") from e


@studio.post("/knowledge/reingest")
def knowledge_reingest(path: str):
    from .ingest.pipeline import ingest

    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "bad path")
    category = path.split("/", 1)[0]

    def run(log):
        rep = ingest(settings, category=category, full=True, only_paths=[path], log=log)
        return {"summary": rep.summary(), "uploaded": rep.uploaded_chunks}

    return {"job_id": _start_job("reingest", run, {"category": category, "path": path})}


class ImportRequest(BaseModel):
    category: str
    urls: list[str]
    ingest: bool = True


@studio.post("/knowledge/import-url")
def knowledge_import_url(req: ImportRequest):
    from .ingest.import_url import import_urls
    from .ingest.pipeline import ingest

    if not req.category or "/" in req.category or req.category.startswith((".", "_")):
        raise HTTPException(400, "bad category")

    def run(log):
        saved = import_urls(settings, req.category, req.urls, log=log)
        out = {"saved": saved}
        if req.ingest and saved:
            rep = ingest(settings, category=req.category, log=log)
            out["ingest"] = rep.summary()
        return out

    return {"job_id": _start_job("import", run, {"category": req.category, "urls": len(req.urls)})}


class CrawlRequest(BaseModel):
    category: str
    start_urls: list[str]
    include_prefixes: list[str] = []
    max_pages: int = 30
    include_pdfs: bool = True
    ingest: bool = True


@studio.post("/knowledge/crawl")
def knowledge_crawl(req: CrawlRequest):
    from .ingest.crawler import crawl
    from .ingest.pipeline import ingest

    if not req.category or "/" in req.category or req.category.startswith((".", "_")):
        raise HTTPException(400, "bad category")
    if not any(u.strip().startswith("http") for u in req.start_urls):
        raise HTTPException(400, "start_urls must contain at least one http(s) URL")

    def run(log):
        stats = crawl(settings, req.category, [u for u in req.start_urls if u.strip()], include_prefixes=req.include_prefixes or None,
                      max_pages=max(1, min(req.max_pages, 300)), include_pdfs=req.include_pdfs, log=log)
        out = {"crawl": {k: v for k, v in stats.items() if k != "files"}, "files": stats["files"][:50]}
        if req.ingest and (stats["pages"] or stats["pdfs"]):
            rep = ingest(settings, category=req.category, log=log)
            out["ingest"] = rep.summary()
        return out

    return {"job_id": _start_job("crawl", run, {"category": req.category, "start": req.start_urls[0] if req.start_urls else ""})}


@studio.get("/bundle.zip")
def bundle_export(parts: str = "skills,rules,knowledge,evals,config", pdfs: bool = True):
    from .bundle import PARTS, export_bundle

    wanted = [x for x in parts.split(",") if x in PARTS] or list(PARTS)
    name = "bankrag-bundle" + ("" if len(wanted) == len(PARTS) else "-" + "-".join(wanted)) + ".zip"
    return Response(export_bundle(settings, wanted, include_pdfs=pdfs), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@studio.post("/bundle/inspect")
async def bundle_inspect(file: UploadFile = File(...)):
    from .bundle import inspect_bundle

    try:
        return inspect_bundle(settings, await file.read())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {str(e)[:300]}") from e


@studio.post("/bundle", dependencies=[Depends(require_admin)])
async def bundle_import(mode: str = "merge", parts: str = "skills,rules,knowledge,evals,config", ingest: bool = False, sync: bool = False, file: UploadFile = File(...)):
    """Write the bundle, then (optionally) ingest the knowledge categories that changed and sync the skills, as one job."""
    from .bundle import PARTS, import_bundle

    wanted = [x for x in parts.split(",") if x in PARTS] or list(PARTS)
    log: list[str] = []
    try:
        result = import_bundle(settings, await file.read(), mode=mode if mode in ("merge", "replace") else "merge", parts=wanted, log=log.append)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {str(e)[:300]}") from e
    out: dict = {"counts": {k: v for k, v in result.items() if not k.startswith("changed_")}, "changed_skills": result["changed_skills"], "changed_categories": result["changed_categories"], "log": log}
    do_ingest = ingest and bool(result["changed_categories"])
    do_sync = sync and ("skills" in wanted)
    if do_ingest or do_sync:
        def run(job_log):
            for line in log:
                job_log(line)
            summary: dict = {}
            if do_ingest:
                from .ingest.pipeline import ingest as run_ingest

                for cat in result["changed_categories"]:
                    job_log(f"== ingest {cat}")
                    rep = run_ingest(settings, category=cat, log=job_log)
                    summary[f"ingest {cat}"] = rep.summary()
            if do_sync:
                from .foundry_sync import sync_skills

                job_log("== sync skills")
                skills, base = _skills()
                rep = sync_skills(settings, skills, base, log=job_log)
                summary["sync"] = {r.skill_id: f"{r.action} v{r.version}" for r in rep.rows}
            return summary

        out["job_id"] = _start_job("import-bundle", run, {"ingest": result["changed_categories"] if do_ingest else [], "sync": do_sync})
    return out


# ---------------- evals ----------------
@studio.get("/evals/runs")
def eval_runs():
    return SESS.list_eval_runs(settings)


@studio.get("/evals/runs.xlsx")
def eval_runs_xlsx(limit: int = 30):
    from .eval_report import history_workbook

    runs = [SESS.get_eval_run(settings, r["id"]) for r in SESS.list_eval_runs(settings, limit=max(1, min(limit, 100)))]
    data = history_workbook([r for r in runs if r])
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="bankrag-eval-runs.xlsx"'})


@studio.get("/evals/runs/{run_id}.xlsx")
def eval_run_xlsx(run_id: str):
    from .eval_report import run_workbook

    r = SESS.get_eval_run(settings, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return Response(run_workbook(r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="bankrag-eval-{r["set"]}-{run_id}.xlsx"'})


@studio.get("/evals/runs/{run_id}")
def eval_run(run_id: str):
    r = SESS.get_eval_run(settings, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@studio.delete("/evals/runs/{run_id}")
def delete_eval_run(run_id: str):
    if not SESS.delete_eval_run(settings, run_id):
        raise HTTPException(404, "run not found")
    return {"ok": True}


class QualityRequest(BaseModel):
    metrics: list[str] = []
    threshold: float = 0.7
    judge_model: Optional[str] = None
    limit: int = 0


@studio.get("/evals/quality/metrics")
def quality_metrics():
    from .quality_eval import DEFAULT_METRICS, METRICS

    return {"metrics": [{"key": k, "group": g, "label": lbl, "needs_expected": ne, "description": d} for k, (g, lbl, ne, d) in METRICS.items()], "default": DEFAULT_METRICS, "judge_model": settings.judge_model}


@studio.post("/evals/quality")
def run_quality_endpoint(req: QualityRequest):
    from .evals import load_cases
    from .quality_eval import METRICS, run_quality

    keys = [k for k in req.metrics if k in METRICS]
    if req.metrics and not keys:
        raise HTTPException(400, "unknown metrics")

    def run(log):
        skills, _ = _skills()
        cases = load_cases(settings, "quality")
        if req.limit:
            cases = cases[: req.limit]
        r = run_quality(settings, skills, cases, metric_keys=keys or None, threshold=max(0.0, min(req.threshold, 1.0)), judge_model=req.judge_model or None, log=log)
        SESS.save_eval_run(settings, r)
        return r

    return {"job_id": _start_job("eval-quality", run, {"set": "quality", "judge": req.judge_model or settings.judge_model})}


@studio.get("/evals/{set_name}.xlsx")
def eval_set_xlsx(set_name: str):
    from .evals import SETS, cases_workbook, load_cases

    if set_name not in SETS:
        raise HTTPException(404, "unknown eval set")
    return Response(cases_workbook(set_name, load_cases(settings, set_name)), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="bankrag-eval-{set_name}-questions.xlsx"'})


@studio.post("/evals/{set_name}/upload")
async def eval_set_upload(set_name: str, mode: str = "append", file: UploadFile = File(...)):
    """Load questions from an .xlsx or .csv into a set: append (skip duplicates) or replace the whole set."""
    from .evals import SETS, append_cases, parse_cases_file, save_cases

    if set_name not in SETS:
        raise HTTPException(404, "unknown eval set")
    try:
        cases = parse_cases_file(set_name, await file.read(), file.filename or "")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"could not read the file: {type(e).__name__}: {str(e)[:200]}") from e
    if not cases:
        raise HTTPException(400, "no questions found: the first column (or a 'question' column) must hold the question text")
    if mode == "replace":
        saved = save_cases(settings, set_name, cases)
        return {"added": len(saved), "skipped": len(cases) - len(saved), "total": len(saved), "mode": "replace"}
    return {**append_cases(settings, set_name, cases), "mode": "append"}


@studio.get("/evals/{set_name}")
def get_eval_set(set_name: str):
    from .evals import load_cases

    try:
        return load_cases(settings, set_name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@studio.put("/evals/{set_name}")
def put_eval_set(set_name: str, cases: list[dict]):
    from .evals import save_cases

    try:
        return save_cases(settings, set_name, cases)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@studio.post("/evals/run")
def run_eval(set: str):
    from .evals import load_cases, run_rag, run_routing

    if set not in ("routing", "rag"):
        raise HTTPException(400, "set must be routing or rag")

    def run(log):
        skills, _ = _skills()
        cases = load_cases(settings, set)
        r = run_routing(settings, skills, cases, log=log) if set == "routing" else run_rag(settings, skills, cases, log=log)
        SESS.save_eval_run(settings, r)
        return r

    return {"job_id": _start_job(f"eval-{set}", run, {"set": set})}


class CompareRequest(BaseModel):
    skill: str
    models: list[str]
    questions: list[str]


@studio.post("/evals/compare")
def run_compare_endpoint(req: CompareRequest):
    from .evals import run_compare

    skills, base = _skills()
    if req.skill not in skills:
        raise HTTPException(404, "skill not found")
    if len(req.models) < 2 or not req.questions:
        raise HTTPException(400, "need at least 2 models and 1 question")

    def run(log):
        r = run_compare(settings, skills, base, req.skill, req.models[:3], req.questions[:10], log=log)
        SESS.save_eval_run(settings, r)
        return r

    return {"job_id": _start_job("compare", run, {"set": "compare", "skill": req.skill, "models": req.models[:3]})}


# ---------------- feedback + review ----------------
class FeedbackRequest(BaseModel):
    session_id: str
    idx: int
    rating: Optional[str] = None
    comment: str = ""


@app.post("/feedback")
def post_feedback(req: FeedbackRequest, request: Request):
    if not SESS.ID_RE.match(req.session_id):
        raise HTTPException(400, "bad session id")
    try:
        return SESS.save_feedback(settings, req.session_id, req.idx, req.rating, req.comment[:1000], request.cookies.get(TESTER_COOKIE, "") or identity(request)["name"])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/feedback")
def get_feedback(session_id: Optional[str] = None):
    return SESS.list_feedback(settings, session_id)


@studio.get("/feedback.csv")
def feedback_csv():
    return Response(SESS.feedback_csv(settings), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="feedback.csv"'})


# ---------------- pages ----------------
_asset_cache: dict = {"stamp": None, "version": ""}


def _asset_version() -> str:
    """Short hash of the web assets' CONTENT, appended as ?v= so browsers drop cached JS/CSS after a deploy.

    Content rather than mtime: a container image can carry normalised timestamps, and an mtime hash that never changes
    leaves every user on the JS they cached before the deploy. The read is cached on the mtime set, so local edits
    still show up without a restart and a request does not re-hash the files."""
    files = sorted(WEB_DIR.glob("*.js")) + sorted(WEB_DIR.glob("*.css"))
    stamp = tuple((q.name, q.stat().st_size, int(q.stat().st_mtime)) for q in files)
    if _asset_cache["stamp"] != stamp:
        h = hashlib.sha1()
        for q in files:
            h.update(q.name.encode())
            h.update(q.read_bytes())
        _asset_cache.update(stamp=stamp, version=h.hexdigest()[:10])
    return _asset_cache["version"]


def _page(name: str) -> Response:
    version = _asset_version()  # per request: a handful of stat() calls, so local edits show up without a restart
    html = (WEB_DIR / name).read_text(encoding="utf-8")
    html = re.sub(r'(/static/[^"\s?]+\.(?:js|css))"', lambda m: f'{m.group(1)}?v={version}"', html)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/")
def mobile_page(request: Request):
    """The customer app. An account with the external role gets its own chat page here instead (external.html),
    and nothing else: /studio refuses it."""
    if studio_role(request, None) == "external":
        return _page("external.html")
    return _page("mobile.html")


@app.get("/legacy")
def legacy_page():
    return _page("index.html")


def _login_ok_response(password: str, tester: str = "", to: str = "/studio") -> RedirectResponse:
    resp = RedirectResponse(to, status_code=303)
    resp.set_cookie(STUDIO_COOKIE, password, httponly=True, samesite="lax", max_age=12 * 3600)
    if tester:
        resp.set_cookie(TESTER_COOKIE, tester[:40], samesite="lax", max_age=30 * 24 * 3600)
    return resp


@app.get("/studio")
def studio_page(request: Request, key: Optional[str] = None, creds: Optional[HTTPBasicCredentials] = Depends(security)):
    """Entra users on the access list as admin or tester go straight in; others, including the external role (whose
    chat page is at /), see the no-access page. Without SSO: login page / ?key=."""
    who = identity(request)
    if who["email"] and SESS.access_count(settings) > 0:
        role = SESS.get_role(settings, who["email"])
        if role not in STAFF_ROLES:
            return Response(_page("noaccess.html").body, status_code=403, media_type="text/html", headers={"Cache-Control": "no-cache"})
        resp = _page("studio.html")
        if not request.cookies.get(TESTER_COOKIE) and who["name"]:
            resp.set_cookie(TESTER_COOKIE, who["name"][:40], samesite="lax", max_age=30 * 24 * 3600)
        return resp
    if key is not None:
        if not _pw_ok(key):
            return RedirectResponse("/studio/login?error=1", status_code=303)
        return _login_ok_response(key, who["name"])
    if not settings.studio_password:
        raise HTTPException(503, "Studio is disabled: set STUDIO_PASSWORD in .env or STUDIO_ADMINS")
    if not _studio_authed(request, creds):
        return RedirectResponse("/studio/login", status_code=303)
    return _page("studio.html")


@app.get("/studio/login")
def studio_login_page():
    return _page("login.html")


@app.post("/studio/login")
def studio_login(request: Request, password: str = Form(...), tester: str = Form("")):
    tester = tester.strip() or identity(request)["name"]
    if not _pw_ok(password):
        return RedirectResponse("/studio/login?error=1", status_code=303)
    return _login_ok_response(password, tester)


@app.post("/studio/logout")
def studio_logout():
    resp = RedirectResponse("/", status_code=303)  # back to the chat page, not the Studio login form
    resp.delete_cookie(STUDIO_COOKIE)
    return resp


@app.get("/studio/me")
def studio_me(request: Request, creds: Optional[HTTPBasicCredentials] = Depends(security)):
    who = identity(request)
    role = studio_role(request, creds)
    return {"authed": role is not None, "role": role, "tester": request.cookies.get(TESTER_COOKIE, "") or who["name"], "sso": who,
            "access_managed": SESS.access_count(settings) > 0}


# ---------------- Studio access management (admins) ----------------
class AccessEntry(BaseModel):
    email: str
    role: str = "tester"
    name: str = ""


@app.get("/access", dependencies=[Depends(require_studio)])
def access_list(request: Request):
    who = identity(request)
    return {"users": SESS.list_access(settings), "me": who, "seeded_admins": settings.studio_admins, "seeded": _seeded()}


def _seeded() -> dict[str, str]:
    """email -> the environment variable that seeds it. A seeded row is re-created on every start, so removing it
    here would only last until the next deploy; the variable has to change instead."""
    out: dict[str, str] = {}
    for var, emails in (("STUDIO_EXTERNALS", settings.studio_externals), ("STUDIO_TESTERS", settings.studio_testers), ("STUDIO_ADMINS", settings.studio_admins)):
        for e in emails:
            out[e] = var
    return out


@app.post("/access", dependencies=[Depends(require_admin)])
def access_upsert(entry: AccessEntry, request: Request):
    who = identity(request)
    email = entry.email.strip().lower()
    if email == who["email"].lower() and entry.role != "admin":
        raise HTTPException(400, "you cannot demote yourself")
    try:
        return SESS.upsert_access(settings, email, entry.role, entry.name, who["email"] or who["name"] or "password")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/access/{email}", dependencies=[Depends(require_admin)])
def access_delete(email: str, request: Request):
    who = identity(request)
    email = email.strip().lower()
    if email == who["email"].lower():
        raise HTTPException(400, "you cannot remove yourself")
    if email in _seeded():
        raise HTTPException(400, f"this account is seeded by {_seeded()[email]}; change the environment variable to remove it")
    admins = [u for u in SESS.list_access(settings) if u["role"] == "admin"]
    if len(admins) == 1 and admins[0]["email"] == email:
        raise HTTPException(400, "cannot remove the last admin")
    if not SESS.delete_access(settings, email):
        raise HTTPException(404, "not on the list")
    return {"ok": True}


app.include_router(studio)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Live-service tools for the agents (FX today, branches next). In Azure the path stays behind Easy Auth and the caller
# is pinned to the Foundry project's managed identity (MCP_CALLER_PRINCIPALS); MCP_TOOL_KEY is the local-dev guard.
if settings.services_mcp_key or settings.services_mcp_callers:
    from contextlib import asynccontextmanager

    from .mcp_server import MCP_PATH, build_asgi

    _mcp_app, _mcp_inner = build_asgi(settings)
    app.mount(MCP_PATH, _mcp_app)
    _app_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan_with_mcp(a):
        # Mount does not run a mounted app's lifespan, and the MCP transport needs its task group started
        async with _mcp_inner.router.lifespan_context(_mcp_inner):
            async with _app_lifespan(a):
                yield

    app.router.lifespan_context = _lifespan_with_mcp


@app.on_event("startup")
def _startup_handoff_reconcile() -> None:
    if not os.environ.get("BANKRAG_NO_RECONCILE"):
        _start_reconcile_thread()

