"""审计日志。"""

from __future__ import annotations

import logging
import sqlite3
import threading

from ..config import get_settings

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def audit(action: str, *, pipeline: str = "local", actor: str = "user",
          doc_id: str | None = None, detail: str | None = None) -> None:
    db = get_settings().db_path
    if not db.exists():
        return
    try:
        with _lock:
            conn = sqlite3.connect(db, timeout=10)
            try:
                conn.execute(
                    "INSERT INTO audit_log (id, ts, action, pipeline, actor, doc_id, detail) "
                    "VALUES (lower(hex(randomblob(16))), "
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?, ?, ?)",
                    (action, pipeline, actor, doc_id, detail),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.exception("audit write failed: %s", action)
