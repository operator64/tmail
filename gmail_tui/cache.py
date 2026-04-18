from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .logging_setup import app_data_dir
from .models import Label, MessageFull, MessageSummary, PendingAction

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    from_addr TEXT,
    subject TEXT,
    snippet TEXT,
    date TEXT,
    labels_json TEXT NOT NULL,
    is_unread INTEGER NOT NULL DEFAULT 0,
    is_starred INTEGER NOT NULL DEFAULT 0,
    has_attachment INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date DESC);

CREATE TABLE IF NOT EXISTS message_bodies (
    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    body_text TEXT,
    body_html TEXT,
    headers_json TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    messages_unread INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS local_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class Cache:
    """Thread-safe SQLite cache. One connection per accessing thread via thread-local."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (app_data_dir() / "cache.db")
        self._local = threading.local()
        self._init_lock = threading.Lock()
        # bootstrap schema on main thread
        self._init_schema()

    # ---------------- Connection management ----------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
                isolation_level=None,  # autocommit; explicit BEGIN/COMMIT via execute
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._init_lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            # Migrations
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "has_attachment" not in cols:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN has_attachment INTEGER NOT NULL DEFAULT 0"
                )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------- sync_state ----------------

    def get_state(self, key: str) -> Optional[str]:
        row = self._conn().execute(
            "SELECT value FROM sync_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._conn().execute(
            "INSERT INTO sync_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------------- Labels ----------------

    def upsert_labels(self, labels: Iterable[Label]) -> None:
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            for lbl in labels:
                conn.execute(
                    "INSERT INTO labels(id,name,type,messages_unread) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type, "
                    "messages_unread=excluded.messages_unread",
                    (lbl.id, lbl.name, lbl.type, lbl.messages_unread),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def list_labels(self) -> list[Label]:
        rows = self._conn().execute(
            "SELECT id,name,type,messages_unread FROM labels"
        ).fetchall()
        return [
            Label(id=r["id"], name=r["name"], type=r["type"], messages_unread=r["messages_unread"])
            for r in rows
        ]

    def delete_label(self, label_id: str) -> None:
        self._conn().execute("DELETE FROM labels WHERE id=?", (label_id,))

    # ---------------- Message summaries ----------------

    def upsert_message_summary(self, m: MessageSummary) -> None:
        self._conn().execute(
            "INSERT INTO messages(id,thread_id,from_addr,subject,snippet,date,labels_json,"
            "is_unread,is_starred,has_attachment,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET thread_id=excluded.thread_id, "
            "from_addr=excluded.from_addr, subject=excluded.subject, snippet=excluded.snippet, "
            "date=excluded.date, labels_json=excluded.labels_json, is_unread=excluded.is_unread, "
            "is_starred=excluded.is_starred, has_attachment=excluded.has_attachment, "
            "fetched_at=excluded.fetched_at",
            (
                m.id,
                m.thread_id,
                m.from_addr,
                m.subject,
                m.snippet,
                _iso(m.date),
                json.dumps(m.labels),
                1 if m.is_unread else 0,
                1 if m.is_starred else 0,
                1 if m.has_attachment else 0,
                _iso(datetime.now(timezone.utc)),
            ),
        )

    def upsert_message_summaries(self, items: Iterable[MessageSummary]) -> None:
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            for m in items:
                self.upsert_message_summary(m)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get_summaries_by_label(
        self, label_id: str, limit: int = 2000
    ) -> list[MessageSummary]:
        rows = self._conn().execute(
            "SELECT * FROM messages WHERE labels_json LIKE ? "
            "ORDER BY COALESCE(date,'') DESC LIMIT ?",
            (f'%"{label_id}"%', limit),
        ).fetchall()
        return [self._row_to_summary(r) for r in rows]

    def get_summary(self, message_id: str) -> Optional[MessageSummary]:
        row = self._conn().execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        return self._row_to_summary(row) if row else None

    def _row_to_summary(self, row: sqlite3.Row) -> MessageSummary:
        has_att = False
        try:
            has_att = bool(row["has_attachment"])
        except (IndexError, KeyError):
            pass
        return MessageSummary(
            id=row["id"],
            thread_id=row["thread_id"],
            from_addr=row["from_addr"] or "",
            subject=row["subject"] or "",
            snippet=row["snippet"] or "",
            date=_parse_iso(row["date"]),
            labels=json.loads(row["labels_json"] or "[]"),
            has_attachment=has_att,
        )

    def update_message_labels(
        self,
        message_id: str,
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
    ) -> None:
        row = self._conn().execute(
            "SELECT labels_json FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if not row:
            return
        labels = set(json.loads(row["labels_json"] or "[]"))
        if add:
            labels.update(add)
        if remove:
            labels.difference_update(remove)
        self._conn().execute(
            "UPDATE messages SET labels_json=?, is_unread=?, is_starred=? WHERE id=?",
            (
                json.dumps(sorted(labels)),
                1 if "UNREAD" in labels else 0,
                1 if "STARRED" in labels else 0,
                message_id,
            ),
        )

    def delete_message(self, message_id: str) -> None:
        self._conn().execute("DELETE FROM messages WHERE id=?", (message_id,))

    # ---------------- Message bodies ----------------

    def upsert_message_full(self, m: MessageFull) -> None:
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO messages(id,thread_id,from_addr,subject,snippet,date,labels_json,"
                "is_unread,is_starred,has_attachment,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET thread_id=excluded.thread_id, "
                "from_addr=excluded.from_addr, subject=excluded.subject, snippet=excluded.snippet, "
                "date=excluded.date, labels_json=excluded.labels_json, is_unread=excluded.is_unread, "
                "is_starred=excluded.is_starred, has_attachment=excluded.has_attachment, "
                "fetched_at=excluded.fetched_at",
                (
                    m.id,
                    m.thread_id,
                    m.from_addr,
                    m.subject,
                    m.body_text[:200] if m.body_text else "",
                    _iso(m.date),
                    json.dumps(m.labels),
                    1 if "UNREAD" in m.labels else 0,
                    1 if "STARRED" in m.labels else 0,
                    1 if m.attachments else 0,
                    _iso(datetime.now(timezone.utc)),
                ),
            )
            conn.execute(
                "INSERT INTO message_bodies(message_id,body_text,body_html,headers_json,fetched_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(message_id) DO UPDATE SET body_text=excluded.body_text, "
                "body_html=excluded.body_html, headers_json=excluded.headers_json, "
                "fetched_at=excluded.fetched_at",
                (
                    m.id,
                    m.body_text,
                    m.body_html,
                    json.dumps(m.headers),
                    _iso(datetime.now(timezone.utc)),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get_message_body(self, message_id: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT body_text,body_html,headers_json FROM message_bodies WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "body_text": row["body_text"] or "",
            "body_html": row["body_html"] or "",
            "headers": json.loads(row["headers_json"] or "{}"),
        }

    # ---------------- Pending actions ----------------

    def add_pending(self, action_type: str, payload: dict) -> int:
        cur = self._conn().execute(
            "INSERT INTO pending_actions(action_type,payload_json,created_at) VALUES(?,?,?)",
            (action_type, json.dumps(payload), _iso(datetime.now(timezone.utc))),
        )
        return cur.lastrowid

    def list_pending(self) -> list[PendingAction]:
        rows = self._conn().execute(
            "SELECT id,action_type,payload_json,created_at,attempts FROM pending_actions "
            "ORDER BY id ASC"
        ).fetchall()
        out: list[PendingAction] = []
        for r in rows:
            out.append(PendingAction(
                id=r["id"],
                action_type=r["action_type"],
                payload=json.loads(r["payload_json"]),
                created_at=_parse_iso(r["created_at"]) or datetime.now(timezone.utc),
                attempts=r["attempts"],
            ))
        return out

    def bump_pending_attempts(self, pending_id: int) -> None:
        self._conn().execute(
            "UPDATE pending_actions SET attempts=attempts+1 WHERE id=?",
            (pending_id,),
        )

    def remove_pending(self, pending_id: int) -> None:
        self._conn().execute("DELETE FROM pending_actions WHERE id=?", (pending_id,))

    # ---------------- Local drafts ----------------

    def save_local_draft(self, draft_id: Optional[int], payload: dict) -> int:
        conn = self._conn()
        now = _iso(datetime.now(timezone.utc))
        if draft_id is None:
            cur = conn.execute(
                "INSERT INTO local_drafts(payload_json,updated_at) VALUES(?,?)",
                (json.dumps(payload), now),
            )
            return cur.lastrowid
        conn.execute(
            "UPDATE local_drafts SET payload_json=?, updated_at=? WHERE id=?",
            (json.dumps(payload), now, draft_id),
        )
        return draft_id

    def delete_local_draft(self, draft_id: int) -> None:
        self._conn().execute("DELETE FROM local_drafts WHERE id=?", (draft_id,))

    def wipe(self) -> None:
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            for t in ("messages", "message_bodies", "labels", "sync_state"):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
