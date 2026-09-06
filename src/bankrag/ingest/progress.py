"""Structured progress for long-running jobs. Loggers passed to ingest/crawl/import may carry a `.progress` attribute
(see api._start_job); plain `print`-style loggers simply get nothing extra."""
from __future__ import annotations

from typing import Any, Callable, Optional

Log = Callable[[str], None]


def report(log: Log, phase: str, done: Optional[int] = None, total: Optional[int] = None, message: Optional[str] = None, **stats: Any) -> None:
    fn = getattr(log, "progress", None)
    if fn:
        fn(phase=phase, done=done, total=total, message=message, **stats)
