"""LangGraph checkpoints (conversation memory) in the same SQLite file as the sessions.

One `SqliteSaver` per database path: it keeps its own connection and its own lock, so it is not wrapped in
`sessions._lock`; the file is already in WAL mode, so the two connections coexist. The tables it creates
(`checkpoints`, `writes`) live next to `sessions` / `turns` and travel with the backup.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver

from .config import Settings
from .sessions import db_path

_savers: dict[str, SqliteSaver] = {}
_lock = threading.Lock()


def new_thread_id() -> str:
    return "thr_" + uuid.uuid4().hex[:16]


def saver(settings: Settings) -> SqliteSaver:
    path = db_path(settings)
    key = str(path)
    with _lock:
        s = _savers.get(key)
        if s is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path, check_same_thread=False, timeout=30)
            s = SqliteSaver(con)
            s.setup()
            _savers[key] = s
    return s


def has_thread(saver_: SqliteSaver, thread_id: str) -> bool:
    try:
        return saver_.get_tuple({"configurable": {"thread_id": thread_id}}) is not None
    except Exception:  # noqa: BLE001
        return False


def delete_thread(saver_: SqliteSaver, thread_id: str) -> None:
    try:
        saver_.delete_thread(thread_id)
    except Exception:  # noqa: BLE001 - a missing thread is not an error
        pass
