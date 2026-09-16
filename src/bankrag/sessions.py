"""Chat session persistence in SQLite (.state/bankrag.db) so follow-up questions survive restarts.

Tables:
  sessions(id, title, created_at, updated_at, conversation_id, prev_skill, turn_count, data)  -- data = full JSON record
  turns(session_id, idx, role, at, text, skill_id, confidence, language, agent_name, route_reason,
        input_tokens, output_tokens, total_ms, retrieved_docs, data)                              -- one row per message, for analysis
Existing .state/sessions/*.json files (older versions) are imported once.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import Settings
from .models import Answer, SessionRecord, Turn

ID_RE = re.compile(r"^[a-f0-9]{12}$")
DB_NAME = "bankrag.db"
_lock = threading.Lock()
_migrated: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, conversation_id TEXT, prev_skill TEXT,
  turn_count INTEGER DEFAULT 0, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated_at DESC);
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY, set_name TEXT, started_at TEXT, finished_at TEXT, summary TEXT, rows TEXT);
CREATE TABLE IF NOT EXISTS feedback (
  session_id TEXT NOT NULL, idx INTEGER NOT NULL, rating TEXT, comment TEXT, tester TEXT, at TEXT,
  PRIMARY KEY (session_id, idx));
CREATE TABLE IF NOT EXISTS studio_access (
  email TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT DEFAULT '', added_by TEXT DEFAULT '', at TEXT);
CREATE TABLE IF NOT EXISTS turns (
  session_id TEXT NOT NULL, idx INTEGER NOT NULL, role TEXT, at TEXT, text TEXT, skill_id TEXT, confidence REAL, language TEXT,
  agent_name TEXT, route_reason TEXT, input_tokens INTEGER, output_tokens INTEGER, total_ms INTEGER, retrieved_docs INTEGER, cost_usd REAL, data TEXT,
  PRIMARY KEY (session_id, idx));
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def db_path(settings: Settings) -> Path:
    """SQLITE_DB_PATH lets the DB live on local disk (network shares such as Azure Files cannot lock SQLite reliably)."""
    override = os.environ.get("SQLITE_DB_PATH", "").strip()
    return Path(override) if override else settings.state_dir / DB_NAME


def backup_db(settings: Settings, dest: Path) -> bool:
    """Consistent online copy of the DB (sqlite backup API) to `dest`; returns False when there is nothing to copy."""
    src = db_path(settings)
    if not src.exists():
        return False
    import shutil
    import tempfile

    dest.parent.mkdir(parents=True, exist_ok=True)
    # 1) consistent snapshot on LOCAL disk (SQLite must not write onto an SMB share), 2) plain byte copy to the share
    fd, local_tmp = tempfile.mkstemp(prefix="bankrag-", suffix=".db", dir=str(src.parent))
    os.close(fd)
    try:
        with _lock:
            con = sqlite3.connect(src, timeout=30)
            try:
                bck = sqlite3.connect(local_tmp)
                try:
                    con.backup(bck)
                finally:
                    bck.close()
            finally:
                con.close()
        share_tmp = dest.with_suffix(dest.suffix + ".tmp")
        shutil.copyfile(local_tmp, share_tmp)
        os.replace(share_tmp, dest)
    finally:
        try:
            os.remove(local_tmp)
        except OSError:
            pass
    return True


def restore_db(settings: Settings, src: Path) -> bool:
    """Copy a backup into place if the working DB does not exist yet (first boot on a fresh container)."""
    dst = db_path(settings)
    if dst.exists() or not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(src, dst)
    return True


def start_backup_thread(settings: Settings, dest: Path, interval_s: int = 60) -> threading.Thread:
    """Background copy of the DB to `dest` (e.g. the mounted share) whenever it changed."""
    def loop() -> None:
        last = None
        while True:
            try:
                src = db_path(settings)
                stamp = (src.stat().st_mtime, src.stat().st_size) if src.exists() else None
                if stamp and stamp != last:
                    backup_db(settings, dest)
                    last = stamp
            except Exception:  # noqa: BLE001 - never kill the thread
                pass
            time.sleep(interval_s)

    t = threading.Thread(target=loop, name="db-backup", daemon=True)
    t.start()
    return t


_journal_mode: dict[str, str] = {}


@contextlib.contextmanager
def connect(settings: Settings):
    """Open, yield, commit, and always CLOSE (an unclosed connection keeps a lock; fatal on SMB shares like Azure Files).
    WAL needs shared memory that network shares lack, so fall back to the rollback journal there."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    db_path(settings).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path(settings), timeout=30, check_same_thread=False, isolation_level="DEFERRED")
    try:
        key = str(db_path(settings))
        mode = _journal_mode.get(key)
        if mode is None and os.environ.get("SQLITE_JOURNAL_MODE", "").lower() == "delete":
            con.execute("PRAGMA journal_mode=DELETE")
            mode = "delete"
        if mode is None:
            try:
                mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise sqlite3.OperationalError("wal unavailable")
            except sqlite3.OperationalError:
                con.execute("PRAGMA journal_mode=DELETE")
                mode = "delete"
            _journal_mode[key] = str(mode).lower()
        con.execute("PRAGMA synchronous=NORMAL")
        _init_schema(settings, con)
        yield con
        con.commit()
    finally:
        con.close()


_schema_done: set[str] = set()


def _init_schema(settings: Settings, con: sqlite3.Connection) -> None:
    key = str(db_path(settings))
    if key in _schema_done:
        return
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    if "source" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'app'")
    for col in ("user_name", "user_email"):
        if col not in cols:
            con.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT DEFAULT ''")
    tcols = {r[1] for r in con.execute("PRAGMA table_info(turns)")}
    if "cost_usd" not in tcols:
        con.execute("ALTER TABLE turns ADD COLUMN cost_usd REAL")
    if "by" not in tcols:
        con.execute("ALTER TABLE turns ADD COLUMN by TEXT DEFAULT ''")
    for email in settings.studio_admins:  # STUDIO_ADMINS env seeds (never demotes) admins so nobody is locked out
        con.execute("INSERT OR IGNORE INTO studio_access(email,role,name,added_by,at) VALUES(?,?,?,?,?)", (email, "admin", "", "STUDIO_ADMINS", _now()))
        con.execute("UPDATE studio_access SET role='admin' WHERE email=? AND role<>'admin'", (email,))
    # STUDIO_TESTERS / STUDIO_EXTERNALS seed a row when it is absent (never change an existing role, so an admin may
    # promote or demote them); since a removed row would come back here, the API refuses to delete seeded accounts.
    for email in getattr(settings, "studio_testers", []):
        con.execute("INSERT OR IGNORE INTO studio_access(email,role,name,added_by,at) VALUES(?,?,?,?,?)", (email, "tester", "", "STUDIO_TESTERS", _now()))
    for email in getattr(settings, "studio_externals", []):  # external: the chat page only
        con.execute("INSERT OR IGNORE INTO studio_access(email,role,name,added_by,at) VALUES(?,?,?,?,?)", (email, "external", "", "STUDIO_EXTERNALS", _now()))
    _import_legacy_json(settings, con)
    con.commit()
    _schema_done.add(key)


def _import_legacy_json(settings: Settings, con: sqlite3.Connection) -> None:
    key = str(settings.state_dir)
    if key in _migrated:
        return
    _migrated.add(key)
    legacy = settings.state_dir / "sessions"
    if not legacy.exists():
        return
    for p in sorted(legacy.glob("*.json")):
        try:
            rec = SessionRecord.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if con.execute("SELECT 1 FROM sessions WHERE id=?", (rec.id,)).fetchone() is None:
            _write(con, rec)
        p.rename(p.with_suffix(".json.imported"))
    con.commit()


def _write(con: sqlite3.Connection, rec: SessionRecord) -> None:
    con.execute(
        "INSERT OR REPLACE INTO sessions(id,title,created_at,updated_at,conversation_id,prev_skill,turn_count,data,source,user_name,user_email) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (rec.id, rec.title, rec.created_at, rec.updated_at, rec.conversation_id, rec.prev_skill, len(rec.turns), rec.model_dump_json(), rec.source, rec.user_name, rec.user_email),
    )
    con.execute("DELETE FROM turns WHERE session_id=?", (rec.id,))
    for i, t in enumerate(rec.turns):
        tr = t.trace or {}
        usage = (tr.get("usage") or {}).get("total") or {}
        con.execute(
            "INSERT INTO turns(session_id,idx,role,at,text,skill_id,confidence,language,agent_name,route_reason,input_tokens,output_tokens,total_ms,retrieved_docs,cost_usd,data,by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, i, t.role, t.at, t.text, t.skill_id, t.confidence, t.language, t.agent_name, t.route_reason,
             usage.get("input_tokens"), usage.get("output_tokens"), (tr.get("timings_ms") or {}).get("total"), (tr.get("retrieval") or {}).get("documents"),
             (tr.get("cost") or {}).get("total_usd"), t.model_dump_json(), t.by),
        )


def session_path(settings: Settings, sid: str) -> Path:  # kept for compatibility with older callers
    if not ID_RE.match(sid):
        raise ValueError("bad session id")
    return settings.state_dir / "sessions" / f"{sid}.json"


def new_record(sid: Optional[str] = None) -> SessionRecord:
    now = _now()
    return SessionRecord(id=sid or new_session_id(), created_at=now, updated_at=now)


def load_session(settings: Settings, sid: str) -> Optional[SessionRecord]:
    if not ID_RE.match(sid):
        raise ValueError("bad session id")
    with _lock, connect(settings) as con:
        row = con.execute("SELECT data FROM sessions WHERE id=?", (sid,)).fetchone()
    return SessionRecord.model_validate_json(row[0]) if row else None


def save_session(settings: Settings, rec: SessionRecord) -> None:
    with _lock, connect(settings) as con:
        _write(con, rec)
        con.commit()


def delete_session(settings: Settings, sid: str) -> bool:
    if not ID_RE.match(sid):
        raise ValueError("bad session id")
    with _lock, connect(settings) as con:
        cur = con.execute("DELETE FROM sessions WHERE id=?", (sid,))
        con.execute("DELETE FROM turns WHERE session_id=?", (sid,))
        con.commit()
        return cur.rowcount > 0


def list_sessions(settings: Settings, limit: int = 50, *, owner_email: str = "", owner_name: str = "") -> list[dict]:
    """Recent sessions; with owner_email/owner_name only that person's sessions (email match, or name match for
    sessions saved before emails were recorded)."""
    sql = "SELECT id,title,updated_at,created_at,turn_count,prev_skill,user_name FROM sessions"
    args: list = []
    if owner_email or owner_name:
        sql += " WHERE (user_email<>'' AND lower(user_email)=lower(?)) OR (user_email='' AND user_name<>'' AND user_name=?)"
        args += [owner_email, owner_name]
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    with _lock, connect(settings) as con:
        rows = con.execute(sql, args).fetchall()
    return [{"id": r[0], "title": r[1], "updated_at": r[2], "created_at": r[3], "turns": r[4], "prev_skill": r[5], "user_name": r[6] or ""} for r in rows]


def stats(settings: Settings) -> dict:
    """Aggregate usage for the Studio: sessions, turns, tokens, latency, per-skill counts."""
    with _lock, connect(settings) as con:
        sessions, = con.execute("SELECT COUNT(*) FROM sessions").fetchone()
        turns, tok_in, tok_out, avg_ms, cost = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(AVG(total_ms),0), COALESCE(SUM(cost_usd),0) FROM turns WHERE role='assistant'"
        ).fetchone()
        per_day = con.execute("SELECT substr(at,1,10) d, COUNT(*), COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens),0) FROM turns WHERE role='assistant' GROUP BY d ORDER BY d DESC LIMIT 14").fetchall()
        per_skill = con.execute("SELECT skill_id, COUNT(*) FROM turns WHERE role='assistant' GROUP BY skill_id ORDER BY 2 DESC").fetchall()
        per_lang = con.execute("SELECT language, COUNT(*) FROM turns WHERE role='assistant' GROUP BY language").fetchall()
    return {"db": str(db_path(settings)), "sessions": sessions, "answers": turns, "input_tokens": tok_in, "output_tokens": tok_out,
            "avg_total_ms": int(avg_ms or 0), "cost_usd": float(cost or 0), "avg_cost_usd": (float(cost or 0) / turns) if turns else 0.0,
            "per_skill": dict(per_skill), "per_language": dict(per_lang),
            "per_day": [{"day": r[0], "answers": r[1], "cost_usd": r[2], "input_tokens": r[3]} for r in per_day]}


def append_turns(rec: SessionRecord, question: str, answer: Answer, by: str = "") -> None:
    now = _now()
    if not rec.title:
        rec.title = question.strip()[:60]
    rec.turns.append(Turn(role="user", text=question, at=now, language=answer.language, by=by or rec.user_name))
    rec.turns.append(
        Turn(
            role="assistant", text=answer.text, at=now, skill_id=answer.skill_id, confidence=answer.confidence,
            citations=answer.citations, references=answer.references, suggestions=answer.suggestions, language=answer.language,
            route_reason=answer.route_reason, agent_name=answer.agent_name, tool_calls=answer.tool_calls, trace=answer.trace,
        )
    )
    rec.updated_at = now


# ---- review + feedback ----
def review_list(settings: Settings, *, skill: str = "", rating: str = "", user: str = "", limit: int = 200) -> list[dict]:
    with _lock, connect(settings) as con:
        rows = con.execute(
            "SELECT s.id, s.title, s.created_at, s.updated_at, s.turn_count, s.source, "
            "(SELECT COALESCE(SUM(cost_usd),0) FROM turns t WHERE t.session_id=s.id) AS cost, "
            "(SELECT GROUP_CONCAT(DISTINCT skill_id) FROM turns t WHERE t.session_id=s.id AND t.role='assistant') AS skills, "
            "(SELECT COUNT(*) FROM feedback f WHERE f.session_id=s.id AND f.rating='up') AS up, "
            "(SELECT COUNT(*) FROM feedback f WHERE f.session_id=s.id AND f.rating='down') AS down, s.user_name, s.user_email "
            "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        skills = [x for x in (r[7] or "").split(",") if x]
        if skill and skill not in skills:
            continue
        if user and user not in ((r[10] or ""), (r[11] or "")):
            continue
        if rating == "up" and not r[8]:
            continue
        if rating == "down" and not r[9]:
            continue
        out.append({"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3], "turns": r[4], "source": r[5] or "app", "cost_usd": r[6], "skills": skills, "up": r[8], "down": r[9], "user_name": r[10] or "", "user_email": r[11] or ""})
    return out


def question_rows(settings: Settings, *, skill: str = "", rating: str = "", q: str = "", source: str = "", user: str = "", comment: str = "",
                  limit: int = 500, items: Optional[list[tuple[str, int]]] = None) -> list[dict]:
    """Flat customer-question / answer pairs across sessions (newest first) with feedback, for review, selection and export.
    `comment`: "any" = has a comment, "none" = rated without one, anything else = the comment contains that text (a dislike
    reason label such as "Wrong information", since the chat UIs store reasons as "Label; Label — note").
    `items` = [(session_id, user_turn_idx)] restricts the result to those questions (any order), e.g. an export selection."""
    sql = ("SELECT u.session_id, u.idx, u.at, u.text, a.text, a.skill_id, a.language, a.confidence, a.cost_usd, a.total_ms, a.retrieved_docs, a.agent_name, "
           "f.rating, f.comment, f.tester, s.title, s.source, a.input_tokens, a.output_tokens, COALESCE(NULLIF(u.by,''), s.user_name, '') AS asked_by, s.user_email "
           "FROM turns u JOIN turns a ON a.session_id=u.session_id AND a.idx=u.idx+1 AND a.role='assistant' "
           "JOIN sessions s ON s.id=u.session_id LEFT JOIN feedback f ON f.session_id=a.session_id AND f.idx=a.idx WHERE u.role='user'")
    args: list = []
    if skill:
        sql += " AND a.skill_id=?"; args.append(skill)
    if rating in ("up", "down"):
        sql += " AND f.rating=?"; args.append(rating)
    elif rating == "any":
        sql += " AND f.rating IS NOT NULL"
    if q:
        sql += " AND (u.text LIKE ? OR a.text LIKE ? OR f.comment LIKE ?)"; args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if comment == "any":
        sql += " AND COALESCE(f.comment,'')<>''"
    elif comment == "none":
        sql += " AND f.rating IS NOT NULL AND COALESCE(f.comment,'')=''"
    elif comment:
        sql += " AND f.comment LIKE ?"; args.append(f"%{comment}%")
    if source:
        sql += " AND COALESCE(s.source,'app')=?"; args.append(source)
    if user:
        sql += " AND (u.by=? OR s.user_name=? OR s.user_email=?)"; args += [user, user, user]
    if items:
        sql += " AND (" + " OR ".join("(u.session_id=? AND u.idx=?)" for _ in items) + ")"
        for sid, idx in items:
            args += [sid, idx]
    sql += " ORDER BY u.at DESC LIMIT ?"; args.append(max(1, min(limit, 5000)))
    with _lock, connect(settings) as con:
        rows = con.execute(sql, args).fetchall()
    return [{"session_id": r[0], "idx": r[1], "at": r[2], "question": r[3], "answer": r[4], "skill_id": r[5] or "", "language": r[6] or "", "confidence": r[7],
             "cost_usd": r[8], "total_ms": r[9], "retrieved_docs": r[10], "agent_name": r[11] or "", "rating": r[12], "comment": r[13] or "", "tester": r[14] or "",
             "session_title": r[15] or "", "source": r[16] or "app", "input_tokens": r[17], "output_tokens": r[18], "user": r[19] or "", "user_email": r[20] or ""} for r in rows]


def known_users(settings: Settings) -> list[str]:
    with _lock, connect(settings) as con:
        rows = con.execute("SELECT DISTINCT user_name FROM sessions WHERE user_name<>'' UNION SELECT DISTINCT by FROM turns WHERE by<>'' ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def save_feedback(settings: Settings, session_id: str, idx: int, rating: Optional[str], comment: str, tester: str) -> dict:
    if rating not in (None, "", "up", "down"):
        raise ValueError("rating must be up, down or empty")
    with _lock, connect(settings) as con:
        con.execute("INSERT OR REPLACE INTO feedback(session_id,idx,rating,comment,tester,at) VALUES(?,?,?,?,?,?)", (session_id, idx, rating or None, comment or "", tester or "", _now()))
        con.commit()
    return {"session_id": session_id, "idx": idx, "rating": rating or None, "comment": comment or "", "tester": tester or ""}


def list_feedback(settings: Settings, session_id: Optional[str] = None) -> list[dict]:
    with _lock, connect(settings) as con:
        q = "SELECT session_id, idx, rating, comment, tester, at FROM feedback" + (" WHERE session_id=?" if session_id else "") + " ORDER BY at DESC"
        rows = con.execute(q, (session_id,) if session_id else ()).fetchall()
    return [{"session_id": r[0], "idx": r[1], "rating": r[2], "comment": r[3], "tester": r[4], "at": r[5]} for r in rows]


def feedback_csv(settings: Settings) -> str:
    import csv
    import io

    with _lock, connect(settings) as con:
        rows = con.execute(
            "SELECT f.at, f.tester, f.rating, f.comment, f.session_id, f.idx, t.skill_id, t.language, t.cost_usd, t.total_ms, "
            "(SELECT text FROM turns u WHERE u.session_id=f.session_id AND u.idx=f.idx-1) AS question, t.text "
            "FROM feedback f LEFT JOIN turns t ON t.session_id=f.session_id AND t.idx=f.idx ORDER BY f.at DESC").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "tester", "rating", "comment", "session_id", "turn", "skill", "language", "cost_usd", "total_ms", "question", "answer"])
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


# ---- eval runs ----
def save_eval_run(settings: Settings, run: dict) -> None:
    with _lock, connect(settings) as con:
        con.execute("INSERT OR REPLACE INTO eval_runs(id,set_name,started_at,finished_at,summary,rows) VALUES(?,?,?,?,?,?)",
                    (run["id"], run["set"], run["started_at"], run.get("finished_at"), json.dumps(run.get("summary", {}), ensure_ascii=False), json.dumps(run.get("rows", []), ensure_ascii=False)))
        con.commit()


def list_eval_runs(settings: Settings, limit: int = 50) -> list[dict]:
    with _lock, connect(settings) as con:
        rows = con.execute("SELECT id,set_name,started_at,finished_at,summary FROM eval_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r[0], "set": r[1], "started_at": r[2], "finished_at": r[3], "summary": json.loads(r[4] or "{}")} for r in rows]


def delete_eval_run(settings: Settings, run_id: str) -> bool:
    with _lock, connect(settings) as con:
        cur = con.execute("DELETE FROM eval_runs WHERE id=?", (run_id,))
        con.commit()
        return cur.rowcount > 0


def get_eval_run(settings: Settings, run_id: str) -> Optional[dict]:
    with _lock, connect(settings) as con:
        r = con.execute("SELECT id,set_name,started_at,finished_at,summary,rows FROM eval_runs WHERE id=?", (run_id,)).fetchone()
    return {"id": r[0], "set": r[1], "started_at": r[2], "finished_at": r[3], "summary": json.loads(r[4] or "{}"), "rows": json.loads(r[5] or "[]")} if r else None


# ---------------- Studio access list (Entra identities) ----------------
ROLES = ("admin", "tester", "external")  # external: the chat page only (a debug chat with the trace), no Studio tabs


def list_access(settings: Settings) -> list[dict]:
    with _lock, connect(settings) as con:
        rows = con.execute("SELECT email, role, name, added_by, at FROM studio_access ORDER BY role, email").fetchall()
    return [{"email": r[0], "role": r[1], "name": r[2] or "", "added_by": r[3] or "", "at": r[4] or ""} for r in rows]


def get_role(settings: Settings, email: str) -> Optional[str]:
    if not email:
        return None
    with _lock, connect(settings) as con:
        r = con.execute("SELECT role FROM studio_access WHERE email=?", (email.strip().lower(),)).fetchone()
    return r[0] if r else None


def access_count(settings: Settings) -> int:
    with _lock, connect(settings) as con:
        return int(con.execute("SELECT COUNT(*) FROM studio_access").fetchone()[0])


def upsert_access(settings: Settings, email: str, role: str, name: str = "", added_by: str = "") -> dict:
    email = email.strip().lower()
    if "@" not in email or " " in email:
        raise ValueError("enter an email address")
    if role not in ROLES:
        raise ValueError("role must be admin, tester or external")
    with _lock, connect(settings) as con:
        con.execute("INSERT INTO studio_access(email,role,name,added_by,at) VALUES(?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET role=excluded.role, name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE studio_access.name END, added_by=excluded.added_by, at=excluded.at",
                    (email, role, name.strip(), added_by, _now()))
        con.commit()
    return {"email": email, "role": role}


def delete_access(settings: Settings, email: str) -> bool:
    with _lock, connect(settings) as con:
        cur = con.execute("DELETE FROM studio_access WHERE email=?", (email.strip().lower(),))
        con.commit()
        return cur.rowcount > 0


# ---------------- handoff usage reconciliation ----------------
def pending_handoff_turns(settings: Settings, hours: int = 24, limit: int = 200) -> list[tuple[str, int]]:
    """(session_id, idx) of assistant turns whose A2A specialist usage has not been read from the trace yet."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _lock, connect(settings) as con:
        rows = con.execute("SELECT session_id, idx FROM turns WHERE role='assistant' AND at > ? AND (data LIKE '%usage_pending%:true%' OR data LIKE '%usage_pending%: true%') ORDER BY at DESC LIMIT ?", (since, limit)).fetchall()
    return [(r[0], r[1]) for r in rows]


def update_turn_trace(settings: Settings, session_id: str, idx: int, trace: dict) -> bool:
    """Replace one assistant turn's trace (and the derived columns) inside the stored session."""
    rec = load_session(settings, session_id)
    if rec is None or idx >= len(rec.turns):
        return False
    rec.turns[idx].trace = trace
    save_session(settings, rec)
    return True

