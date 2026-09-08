from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db, table_counts


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


def test_init_db_creates_all_tables(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    table_names = {row["name"] for row in rows}

    assert table_names == {
        "companies",
        "contacts",
        "email_queue",
        "send_log",
        "send_config",
    }


def test_init_db_seeds_send_config_from_settings(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM send_config WHERE id = 1").fetchone()

    assert row["daily_cap"] == settings.send.start_daily_cap
    assert row["ramp_step"] == settings.send.ramp_step
    assert row["ramp_ceiling"] == settings.send.ramp_ceiling
    assert row["ramp_interval_days"] == settings.send.ramp_interval_days
    assert row["last_ramp_date"] is None


def test_init_db_is_idempotent_and_does_not_reseed(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute("UPDATE send_config SET daily_cap = 999 WHERE id = 1")
        conn.commit()

    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT daily_cap FROM send_config WHERE id = 1").fetchone()

    assert row["daily_cap"] == 999


def test_table_counts_starts_empty(settings):
    init_db(settings.db_path, settings)

    counts = table_counts(settings.db_path)

    assert counts["companies"] == 0
    assert counts["contacts"] == 0
    assert counts["email_queue"] == 0
    assert counts["send_log"] == 0
    assert counts["send_config"] == 1


def test_migrate_backfills_approved_at_for_legacy_approved_rows_only(settings):
    """approved_at (added 2026-09-09) didn't exist when earlier rows were approved --
    init_db's migration should backfill it from created_at for rows that passed
    through 'approved' at some point, but leave still-pending/rejected rows untouched
    since they were never approved.
    """
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name, source) VALUES ('Acme', 'manual_csv')"
        ).lastrowid
        contact_id = conn.execute(
            "INSERT INTO contacts (company_id, email, status) VALUES (?, 'a@acme.com', 'verified')",
            (company_id,),
        ).lastrowid
        approved_id = conn.execute(
            "INSERT INTO email_queue (contact_id, company_id, subject, body, kind, status, "
            "created_at) VALUES (?, ?, 's', 'b', 'initial', 'approved', '2026-01-01T00:00:00')",
            (contact_id, company_id),
        ).lastrowid
        pending_id = conn.execute(
            "INSERT INTO email_queue (contact_id, company_id, subject, body, kind, status, "
            "created_at) VALUES (?, ?, 's', 'b', 'initial', 'pending_review', '2026-01-02T00:00:00')",
            (contact_id, company_id),
        ).lastrowid
        conn.commit()

    init_db(settings.db_path, settings)  # re-run migration, as if against a legacy db

    with connect(settings.db_path) as conn:
        approved_row = conn.execute(
            "SELECT approved_at FROM email_queue WHERE id = ?", (approved_id,)
        ).fetchone()
        pending_row = conn.execute(
            "SELECT approved_at FROM email_queue WHERE id = ?", (pending_id,)
        ).fetchone()

    assert approved_row["approved_at"] == "2026-01-01T00:00:00"
    assert pending_row["approved_at"] is None


def test_foreign_keys_enforced(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contacts (company_id, name) VALUES (?, ?)",
                (999, "Ghost Contact"),
            )
            conn.commit()


def test_companies_domain_uniqueness_enforced(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO companies (name, domain, source) VALUES (?, ?, ?)",
            ("Acme", "acme.com", "manual_csv"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO companies (name, domain, source) VALUES (?, ?, ?)",
                ("Acme Duplicate", "acme.com", "manual_csv"),
            )
            conn.commit()


def test_companies_null_domain_allowed_multiple_times(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO companies (name, domain, source) VALUES (?, ?, ?)",
            ("No Domain A", None, "manual_csv"),
        )
        conn.execute(
            "INSERT INTO companies (name, domain, source) VALUES (?, ?, ?)",
            ("No Domain B", None, "manual_csv"),
        )
        conn.commit()

    counts = table_counts(settings.db_path)
    assert counts["companies"] == 2
