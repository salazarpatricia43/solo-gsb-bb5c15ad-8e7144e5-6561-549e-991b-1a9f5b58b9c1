"""SQLite connection helpers and schema initialization."""
import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "editor.db"))


def get_db() -> sqlite3.Connection:
    # timeout lets a write transaction wait briefly for a competing lock
    # instead of failing immediately with "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS scripts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                content    TEXT NOT NULL DEFAULT '{"start_node_id": null, "nodes": []}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shares (
                token      TEXT PRIMARY KEY,
                script_id  INTEGER,
                script_name TEXT NOT NULL,
                content    TEXT NOT NULL,
                invite_only INTEGER NOT NULL DEFAULT 0,   -- 1 = new answers need an invite code
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                mode            TEXT NOT NULL,            -- 'script' | 'share'
                script_id       INTEGER,
                share_token     TEXT,
                current_node_id TEXT,
                steps           TEXT NOT NULL DEFAULT '[]',
                finished        INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL
            );

            -- One-time pause/resume tokens for read-only share answer sessions.
            -- Each row is bound to exactly one share snapshot (share_token) and
            -- one run; used=1 flips on the single successful resume.
            CREATE TABLE IF NOT EXISTS resume_tokens (
                token       TEXT PRIMARY KEY,
                run_id      INTEGER NOT NULL,
                share_token TEXT NOT NULL,
                used        INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_resume_tokens_run ON resume_tokens(run_id);

            -- Invite codes gating new answer sessions of one share snapshot.
            -- A code belongs to exactly one share (share_token); used=1 flips
            -- atomically in the SAME write transaction that inserts the run,
            -- so verification + consumption + session creation either all
            -- happen or none do.
            CREATE TABLE IF NOT EXISTS invite_codes (
                code        TEXT PRIMARY KEY,
                share_token TEXT NOT NULL,
                used        INTEGER NOT NULL DEFAULT 0,
                run_id      INTEGER,
                created_at  TEXT NOT NULL,
                used_at     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_invite_codes_share
                ON invite_codes(share_token);
            """
        )
        # Backfill old databases created before invite codes existed.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(shares)").fetchall()}
        if "invite_only" not in cols:
            db.execute(
                "ALTER TABLE shares ADD COLUMN invite_only INTEGER NOT NULL DEFAULT 0"
            )
