"""SQLite datastore for the cold-email job agent. See PROJECT.md §6 and db/schema.sql."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent.config import Settings

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_TABLES = ("companies", "contacts", "email_queue", "send_log", "send_config")


def init_db(db_path: Path, settings: Settings | None = None) -> None:
    """Create the schema if it doesn't exist yet, and seed send_config from settings."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema_sql)
        if settings is not None:
            _seed_send_config(conn, settings)
        conn.commit()


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
