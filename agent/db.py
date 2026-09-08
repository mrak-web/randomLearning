"""SQLite datastore for the cold-email job agent. See PROJECT.md §6 and db/schema.sql."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent.config import Settings

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_TABLES = ("companies", "contacts", "email_queue", "send_log", "send_config")


# Columns added after the original schema shipped. init_db adds these to an existing
# agent.db in place (additive-only, no data loss) since CREATE TABLE IF NOT EXISTS
# alone won't touch a table that already exists without these columns.
_ADDED_COLUMNS = {
    "companies": [
        ("outcome_status", "TEXT"),
        ("outcome_notes", "TEXT"),
    ],
    "email_queue": [
        ("followup_number", "INTEGER NOT NULL DEFAULT 0"),
        ("approved_at", "TEXT"),
    ],
}


def init_db(db_path: Path, settings: Settings | None = None) -> None:
    """Create the schema if it doesn't exist yet, and seed send_config from settings."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema_sql)
        _migrate_existing_columns(conn)
        if settings is not None:
            _seed_send_config(conn, settings)
        conn.commit()


def _migrate_existing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
    _backfill_legacy_approved_at(conn)


def _backfill_legacy_approved_at(conn: sqlite3.Connection) -> None:
    """Rows approved before the approved_at column existed (2026-09-09) have no record
    of when they were actually approved -- created_at (draft generation time) is the
    best available proxy, so send order (agent/sending.py) still degrades gracefully to
    generation order for those legacy rows instead of an undefined NULL-first order.
    Safe to run every init_db call: only touches rows still missing approved_at.
    """
    conn.execute(
        "UPDATE email_queue SET approved_at = created_at "
        "WHERE approved_at IS NULL AND status NOT IN ('pending_review', 'rejected')"
    )


def _seed_send_config(conn: sqlite3.Connection, settings: Settings) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO send_config (id, daily_cap, ramp_step, ramp_ceiling, ramp_interval_days)
        VALUES (1, ?, ?, ?, ?)
        """,
        (
            settings.send.start_daily_cap,
            settings.send.ramp_step,
            settings.send.ramp_ceiling,
            settings.send.ramp_interval_days,
        ),
    )


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with row access by column name and FK enforcement on."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def table_counts(db_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connect(db_path) as conn:
        for table in _TABLES:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts
