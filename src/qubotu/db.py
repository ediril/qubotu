"""SQLite storage: one connection, used from the event loop."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
-- AUTOINCREMENT on every table whose ids are kept elsewhere (folders, inbox payloads, agents citing
-- memory ids): a deleted row's id is never reused, so a late write can't land on a newer row.
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    specialty TEXT NOT NULL DEFAULT '',             -- first line of goals, shown as the agent's summary
    goals TEXT NOT NULL DEFAULT '',                 -- the job, as the user wrote it
    provider TEXT NOT NULL DEFAULT 'openai',
    model TEXT NOT NULL DEFAULT '',
    schedule_minutes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',          -- active | paused
    thread_id TEXT,
    thread_provider TEXT,
    thread_tools TEXT,                              -- fingerprint of the tool set the thread was started with
    thread_model TEXT,                              -- model the thread was started or last resumed with
    context_hash TEXT,
    next_run_at TEXT,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,                          -- agent:<id>
    agent_id INTEGER,
    role TEXT NOT NULL,                             -- user | agent | activity | system
    text TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel, id);

CREATE TABLE IF NOT EXISTS outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                             -- finding | report | table | draft | link | question | login
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    data TEXT,
    importance TEXT NOT NULL DEFAULT 'normal',      -- low | normal | high
    blocking INTEGER NOT NULL DEFAULT 0,            -- the agent can't go on until the user acts (shown as a blocker)
    status TEXT NOT NULL DEFAULT 'new',             -- new | seen | done | dismissed | open | answered
    answer TEXT,
    feedback TEXT,
    feedback_pending INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outputs_agent ON outputs(agent_id, id);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent INTEGER NOT NULL,
    to_agent INTEGER NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',         -- pending | running | done | failed
    result TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',              -- fact | preference | project
    agent_id INTEGER,
    pinned INTEGER NOT NULL DEFAULT 0,
    embedding BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL,
    trigger TEXT NOT NULL,                          -- chat | schedule | request | result | answer | manual
    status TEXT NOT NULL DEFAULT 'running',         -- running | completed | failed | interrupted
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_agent ON runs(agent_id, id);

-- Every event that should make an agent run. Rows survive restarts, so accepted work is never lost.
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                             -- chat | answer | manual | schedule | request | result
    payload TEXT NOT NULL DEFAULT '{}',
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',         -- pending | running | stopping | done | failed | cancelled
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS inbox_status ON inbox(status, id);

-- Sites the user logged in to for agents. A host covers its subdomains.
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL UNIQUE COLLATE NOCASE,
    logged_in_at TEXT,
    created_at TEXT NOT NULL
);

-- Which agents may use which site's login.
CREATE TABLE IF NOT EXISTS agent_sites (
    agent_id INTEGER NOT NULL,
    site_id INTEGER NOT NULL,
    PRIMARY KEY (agent_id, site_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def later(minutes: float) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def parse_ts(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        # The web layer's test client runs handlers on another thread; the app itself uses one thread.
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._make_private(self.path, f"{self.path}-wal", f"{self.path}-shm")

    @staticmethod
    def _make_private(*paths: str) -> None:
        """The database holds conversations, memory, and an API key: readable by the owner only."""
        for p in paths:
            if p != ":memory:" and os.path.exists(p):
                os.chmod(p, 0o600)

    @contextmanager
    def transaction(self):
        """Reentrant: only the outermost block commits or rolls back."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def all(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def value(self, sql: str, *args):
        r = self.conn.execute(sql, args).fetchone()
        return r[0] if r else None

    def run(self, sql: str, *args) -> int:
        cur = self.conn.execute(sql, args)
        return cur.lastrowid

    def insert(self, table: str, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *fields.values())

    def update(self, table: str, id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE {table} SET {sets} WHERE id = ?", *fields.values(), id)

    # Settings

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        v = self.value("SELECT value FROM settings WHERE key = ?", key)
        return default if v is None else v

    def set_setting(self, key: str, value: str | None) -> None:
        self.run(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            key,
            value,
        )

    # Agents

    def agent(self, agent_id: int) -> dict | None:
        return self.one("SELECT * FROM agents WHERE id = ?", agent_id)

    def agent_by_name(self, name: str) -> dict | None:
        return self.one("SELECT * FROM agents WHERE name = ?", name.strip().lstrip("@"))

    def agents(self) -> list[dict]:
        return self.all("SELECT * FROM agents ORDER BY name")

    # Messages

    def add_message(self, channel: str, role: str, text: str, agent_id: int | None = None, meta: dict | None = None) -> int:
        return self.insert(
            "messages",
            channel=channel,
            agent_id=agent_id,
            role=role,
            text=text,
            meta=json.dumps(meta) if meta else None,
            created_at=now(),
        )

    def messages(self, channel: str, limit: int = 200) -> list[dict]:
        rows = self.all(
            "SELECT m.*, a.name AS agent_name FROM messages m LEFT JOIN agents a ON a.id = m.agent_id "
            "WHERE channel = ? ORDER BY m.id DESC LIMIT ?",
            channel,
            limit,
        )
        rows.reverse()
        for r in rows:
            r["meta"] = json.loads(r["meta"]) if r["meta"] else {}
        return rows
