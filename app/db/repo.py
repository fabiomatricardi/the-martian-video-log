"""SQLite persistence for video-log entries (thread-safe single connection)."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app.core.config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT UNIQUE,
    sol           INTEGER,
    log_number    INTEGER,
    started_at    TEXT,
    ended_at      TEXT,
    duration_s    REAL,
    video_file    TEXT,
    text_file     TEXT,
    srt_file      TEXT,
    transcript    TEXT,
    segments      TEXT,
    summary       TEXT,
    title         TEXT,
    topics        TEXT,
    mood          TEXT,
    status        TEXT,
    error_msg     TEXT,
    file_size     INTEGER,
    live_model    TEXT,
    summary_model TEXT,
    created_at    TEXT
);
"""

STATUS_RECORDING = "recording"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"


class Repo:
    def __init__(self, db_path: Path | None = None):
        db_path = db_path or (DATA_DIR / "vlog.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- write ops --------------------------------------------------------
    def insert_entry(self, **fields) -> int:
        fields.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
        cols = ", ".join(fields.keys())
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO entries ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update(self, entry_id: int, **fields) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE entries SET {sets} WHERE id = ?", (*fields.values(), entry_id)
            )
            self._conn.commit()

    # -- read ops ---------------------------------------------------------
    def get(self, entry_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM entries ORDER BY started_at DESC, id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def next_log_number(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(log_number) AS m FROM entries"
            ).fetchone()
        m = row["m"] if row else None
        return (m + 1) if m else 1

    def by_status(self, *statuses: str) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM entries WHERE status IN ({marks}) ORDER BY id",
                tuple(statuses),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
