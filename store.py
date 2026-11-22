"""Persistent execution record (SQLite). Used for duplicate protection and status history."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS executions (
                   key        TEXT PRIMARY KEY,
                   event      TEXT NOT NULL,
                   status     TEXT NOT NULL,
                   alert      TEXT,
                   result     TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )

    def claim(self, key: str, event: str, alert: dict) -> Optional[dict]:
        """Atomically record a new alert. Returns None if claimed, or the existing row if duplicate."""
        with self._lock:
            now = _now()
            try:
                self._db.execute(
                    "INSERT INTO executions (key, event, status, alert, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (key, event, "RECEIVED", json.dumps(alert), now, now),
                )
                return None
            except sqlite3.IntegrityError:
                row = self._db.execute("SELECT * FROM executions WHERE key=?", (key,)).fetchone()
                return dict(row)

    def update(self, key: str, status: str, result: Optional[dict] = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE executions SET status=?, result=COALESCE(?, result), updated_at=? WHERE key=?",
                (status, json.dumps(result) if result is not None else None, _now(), key),
            )

    def mark_interrupted(self) -> int:
        """Anything still RECEIVED/PROCESSING at startup was cut off by a restart: never auto-retry it."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE executions SET status='INTERRUPTED', updated_at=? WHERE status IN ('RECEIVED','PROCESSING')",
                (_now(),),
            )
            return cur.rowcount

    def recent(self, limit: int) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, event, status, alert, result, created_at, updated_at "
                "FROM executions ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["key"] = item["key"][:12]
            item["alert"] = json.loads(item["alert"]) if item["alert"] else None
            item["result"] = json.loads(item["result"]) if item["result"] else None
            out.append(item)
        return out
