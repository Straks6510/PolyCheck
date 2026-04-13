"""
PolyCheck — SQLite persistence layer.

Schema
------
events
  id          TEXT  PRIMARY KEY   — Polymarket event id
  title       TEXT
  end_date    TEXT                — ISO-8601 UTC
  volume_24h  REAL
  volume      REAL
  liquidity   REAL
  market_count INTEGER
  category    TEXT
  fetched_at  TEXT                — ISO-8601 UTC, set on every upsert
  raw         TEXT                — full JSON blob for forward-compatibility

favorites
  event_id    TEXT  PRIMARY KEY   — Polymarket event id
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "polycheck.db"

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id            TEXT    PRIMARY KEY,
    title         TEXT,
    end_date      TEXT,
    volume_24h    REAL,
    volume        REAL,
    liquidity     REAL,
    market_count  INTEGER,
    category      TEXT,
    fetched_at    TEXT    NOT NULL,
    raw           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_end_date ON events (end_date);
CREATE TABLE IF NOT EXISTS favorites (
    event_id      TEXT    PRIMARY KEY
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init() -> None:
    """Create the database and tables if they don't exist yet."""
    with _connect() as conn:
        conn.executescript(_DDL)


def upsert_events(events: list[dict[str, Any]]) -> int:
    """
    Insert or replace events.  Returns the number of rows written.
    Existing rows are fully replaced (id is the conflict key).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for e in events:
        tags: list[dict] = e.get("tags") or []
        category = tags[0].get("label", "") if tags else ""
        markets: list[dict] = e.get("markets") or []
        rows.append((
            str(e.get("id", "")),
            e.get("title", ""),
            e.get("endDate", ""),
            _float(e.get("volume24hr")),
            _float(e.get("volume")),
            _float(e.get("liquidity")),
            len(markets),
            category,
            now,
            json.dumps(e, ensure_ascii=False),
        ))

    with _connect() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO events
                (id, title, end_date, volume_24h, volume, liquidity,
                 market_count, category, fetched_at, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
    return len(rows)


def delete_stale_events(current_ids: list[str]) -> int:
    """
    Delete events whose IDs are not in current_ids.
    Returns the number of rows deleted.
    Skips deletion if current_ids is empty (safety guard).
    """
    if not current_ids:
        return 0
    placeholders = ",".join("?" * len(current_ids))
    with _connect() as conn:
        cur = conn.execute(
            f"DELETE FROM events WHERE id NOT IN ({placeholders})",
            current_ids,
        )
        return cur.rowcount


def load_events() -> list[dict[str, Any]]:
    """
    Return all stored events as dicts (the original raw JSON objects),
    ordered by end_date ascending.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT raw FROM events ORDER BY end_date ASC"
        ).fetchall()
    return [json.loads(r["raw"]) for r in rows]


def last_fetched_at() -> str | None:
    """Return the most recent fetched_at timestamp stored, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) AS ts FROM events"
        ).fetchone()
    return row["ts"] if row else None


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

def load_favorites() -> set[str]:
    """Return the set of all favorited event IDs."""
    with _connect() as conn:
        rows = conn.execute("SELECT event_id FROM favorites").fetchall()
    return {r["event_id"] for r in rows}


def add_favorite(event_id: str) -> None:
    """Mark an event as a favorite."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (event_id) VALUES (?)",
            (event_id,),
        )


def remove_favorite(event_id: str) -> None:
    """Remove an event from favorites."""
    with _connect() as conn:
        conn.execute("DELETE FROM favorites WHERE event_id = ?", (event_id,))


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
