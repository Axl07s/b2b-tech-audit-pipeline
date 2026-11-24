"""
db.py — Shared SQLite helpers.
All modules import from here; never open leads.db directly elsewhere.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "leads.db"

# Valid state machine transitions
VALID_STATES = {
    "NEW",
    "AUDITED",
    "NO_WEBSITE_MANUAL",
    "QUALIFIED",
    "DISCARDED",
    "DRAFTED",
    "SENT_1",
    "SENT_2",
    "SENT_3",
    "REPLIED_POSITIVE",
    "REPLIED_NEGATIVE",
    "BOUNCED",
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create schema if it does not exist yet."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS businesses (
                place_id            TEXT PRIMARY KEY,
                niche               TEXT NOT NULL,
                city                TEXT NOT NULL,
                country             TEXT NOT NULL,
                lang                TEXT NOT NULL DEFAULT 'en',
                display_name        TEXT,
                rating              REAL,
                user_rating_count   INTEGER,
                website_uri         TEXT,
                phone               TEXT,
                address             TEXT,
                google_maps_uri     TEXT,
                -- Audit results
                state               TEXT NOT NULL DEFAULT 'NEW',
                detected_problems   TEXT,          -- JSON array of strings
                found_emails        TEXT,          -- JSON array of strings
                wa_link             TEXT,
                audit_notes         TEXT,
                -- Draft content
                subject             TEXT,
                body_day1           TEXT,
                body_day2           TEXT,
                body_day7           TEXT,
                -- Sending metadata
                sent_at_1           TEXT,
                sent_at_2           TEXT,
                sent_at_3           TEXT,
                message_id_1        TEXT,
                message_id_2        TEXT,
                message_id_3        TEXT,
                -- Reply metadata
                reply_classification TEXT,
                reply_snippet       TEXT,
                -- Timestamps
                created_at          TEXT DEFAULT (datetime('now')),
                updated_at          TEXT DEFAULT (datetime('now'))
            );

            CREATE TRIGGER IF NOT EXISTS businesses_updated_at
            AFTER UPDATE ON businesses
            BEGIN
                UPDATE businesses SET updated_at = datetime('now')
                WHERE place_id = NEW.place_id;
            END;

            CREATE TABLE IF NOT EXISTS daily_send_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sent_date   TEXT NOT NULL,
                place_id    TEXT NOT NULL,
                attempt_num INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_state ON businesses(state);
            CREATE INDEX IF NOT EXISTS idx_country ON businesses(country);
            """
        )


def upsert_business(data: dict[str, Any]) -> bool:
    """
    Insert a new business or ignore if place_id already exists.
    Returns True if a new row was inserted.
    """
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    sql = f"INSERT OR IGNORE INTO businesses ({cols}) VALUES ({placeholders})"
    with get_conn() as conn:
        cur = conn.execute(sql, list(data.values()))
        return cur.rowcount > 0


def set_state(place_id: str, state: str, extra: dict[str, Any] | None = None) -> None:
    """Update the state of a business, optionally setting extra columns."""
    if state not in VALID_STATES:
        raise ValueError(f"Invalid state: {state!r}")
    updates = {"state": state}
    if extra:
        updates.update(extra)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    sql = f"UPDATE businesses SET {set_clause} WHERE place_id = ?"
    with get_conn() as conn:
        conn.execute(sql, [*updates.values(), place_id])


def fetch_by_state(state: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM businesses WHERE state = ? ORDER BY created_at", (state,)
        ).fetchall()


def fetch_one(place_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM businesses WHERE place_id = ?", (place_id,)
        ).fetchone()


def count_sent_today() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM daily_send_log WHERE sent_date = date('now')"
        ).fetchone()
        return row[0] if row else 0


def log_send(place_id: str, attempt_num: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO daily_send_log (sent_date, place_id, attempt_num) VALUES (date('now'), ?, ?)",
            (place_id, attempt_num),
        )


def json_loads_safe(value: str | None) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
