"""SQLite persistence. Single file, WAL mode, safe across restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id        TEXT UNIQUE NOT NULL,
    kind            TEXT NOT NULL,            -- post | thread_part | cta_reply | reply
    root_tweet_id   TEXT,
    parent_tweet_id TEXT,
    text            TEXT NOT NULL,
    pillar          TEXT,
    format          TEXT,
    hour_local      INTEGER,
    slot_id         INTEGER,
    offer           TEXT,
    dry_run         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_kind ON posts(kind);

CREATE TABLE IF NOT EXISTS metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id        TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    age_hours       REAL NOT NULL,
    impressions     INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER,
    quotes          INTEGER, bookmarks INTEGER, profile_clicks INTEGER, url_clicks INTEGER,
    score           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_tweet ON metrics(tweet_id, fetched_at);

CREATE TABLE IF NOT EXISTS rewards (
    tweet_id    TEXT PRIMARY KEY,
    pillar      TEXT, format TEXT, hour_local INTEGER,
    reward      REAL NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date_local    TEXT NOT NULL,
    scheduled_at  TEXT NOT NULL,              -- UTC ISO
    hour_local    INTEGER NOT NULL,
    pillar        TEXT NOT NULL,
    format        TEXT NOT NULL,
    cta           INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|posting|done|failed|missed|skipped
    tweet_id      TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_status ON slots(status, scheduled_at);

CREATE TABLE IF NOT EXISTS mentions (
    tweet_id        TEXT PRIMARY KEY,
    author_id       TEXT,
    author_username TEXT,
    text            TEXT,
    conversation_id TEXT,
    created_at      TEXT,
    status          TEXT NOT NULL,            -- replied|skipped|failed
    reason          TEXT,
    reply_tweet_id  TEXT,
    handled_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentions_author ON mentions(author_id, handled_at);

CREATE TABLE IF NOT EXISTS research (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date_local TEXT NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    angle      TEXT,
    url        TEXT,
    source     TEXT,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_title ON research(date_local, title);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,               -- post | reply
    slot_id      INTEGER,
    mention_id   TEXT,
    payload      TEXT NOT NULL,               -- JSON
    status       TEXT NOT NULL,               -- pending|approved|rejected|expired|posted|failed
    tg_message_id INTEGER,
    note         TEXT,
    created_at   TEXT NOT NULL,
    decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

CREATE TABLE IF NOT EXISTS account_snapshots (
    ts        TEXT PRIMARY KEY,
    followers INTEGER,
    following INTEGER,
    posts     INTEGER
);

CREATE TABLE IF NOT EXISTS usage (
    day      TEXT NOT NULL,
    bucket   TEXT NOT NULL,                   -- e.g. x:post, x:read, llm:input_tokens
    amount   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, bucket)
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---- kv -------------------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ---- events / usage -------------------------------------------------------------------------
    def event(self, kind: str, message: str, level: str = "INFO") -> None:
        self.execute(
            "INSERT INTO events(ts,level,kind,message) VALUES(?,?,?,?)",
            (iso(utcnow()), level, kind, message[:4000]),
        )

    def add_usage(self, bucket: str, amount: float = 1.0, day: str | None = None) -> None:
        day = day or utcnow().strftime("%Y-%m-%d")
        self.execute(
            "INSERT INTO usage(day,bucket,amount) VALUES(?,?,?) "
            "ON CONFLICT(day,bucket) DO UPDATE SET amount=amount+excluded.amount",
            (day, bucket, amount),
        )

    def usage_since(self, bucket: str, since_day: str) -> float:
        row = self.one("SELECT COALESCE(SUM(amount),0) AS s FROM usage WHERE bucket=? AND day>=?", (bucket, since_day))
        return float(row["s"]) if row else 0.0

    # ---- posts ----------------------------------------------------------------------------------
    def record_post(
        self,
        *,
        tweet_id: str,
        kind: str,
        text: str,
        root_tweet_id: str | None = None,
        parent_tweet_id: str | None = None,
        pillar: str | None = None,
        fmt: str | None = None,
        hour_local: int | None = None,
        slot_id: int | None = None,
        offer: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO posts(tweet_id,kind,root_tweet_id,parent_tweet_id,text,pillar,format,"
            "hour_local,slot_id,offer,dry_run,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tweet_id, kind, root_tweet_id, parent_tweet_id, text, pillar, fmt,
                hour_local, slot_id, offer, int(dry_run), iso(utcnow()),
            ),
        )

    def recent_texts(self, limit: int) -> list[str]:
        rows = self.query(
            "SELECT text FROM posts WHERE kind IN ('post','thread_part','cta_reply') ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [r["text"] for r in rows]

    def post_by_tweet(self, tweet_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM posts WHERE tweet_id=?", (tweet_id,))

    def count_posts_since(self, since: datetime, kinds: tuple[str, ...]) -> int:
        q = f"SELECT COUNT(*) AS c FROM posts WHERE created_at>=? AND kind IN ({','.join('?' * len(kinds))})"
        row = self.one(q, (iso(since), *kinds))
        return int(row["c"]) if row else 0
