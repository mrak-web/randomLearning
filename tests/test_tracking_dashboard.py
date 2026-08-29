from __future__ import annotations

import dataclasses
import itertools
from datetime import date
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db
from agent.tracking_dashboard import (
    TrackingError,
    dashboard_rows,
    set_company_outcome,
    summarize,
)

_email_counter = itertools.count()


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


def _seed_email_queue_row(
    conn, *, company_name="Meesho", contact_name="Priya Sharma", followup_number=0,
    status="sent", sent_at=None,
) -> tuple[int, int]:
    contact_email = f"contact{next(_email_counter)}@example.com"
    company_id = conn.execute(
        "INSERT INTO companies (name, source, niche, status) VALUES (?, 'manual_csv', 'consumer', 'classified')",
        (company_name,),
    ).lastrowid
    contact_id = conn.execute(
        "INSERT INTO contacts (company_id, name, email, role_category, status) "
        "VALUES (?, ?, ?, 'hr', 'verified')",
        (company_id, contact_name, contact_email),
    ).lastrowid
    conn.execute(
        "INSERT INTO email_queue (contact_id, company_id, niche, subject, body, kind, "
        "followup_number, attached_resume, status, sent_at) "
        "VALUES (?, ?, 'consumer', 'Subject', 'Body', ?, ?, 1, ?, ?)",
        (
            contact_id, company_id, "initial" if followup_number == 0 else "followup",
            followup_number, status, sent_at,
        ),
    )
    conn.commit()
    return company_id, contact_id


# ---------------------------------------------------------------------------
# dashboard_rows
# ---------------------------------------------------------------------------


def test_dashboard_rows_labels_initial_and_followup_stages(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _seed_email_queue_row(conn, followup_number=0, sent_at="2026-08-24T09:00:00")

        rows = dashboard_rows(conn, today=date(2026, 8, 26))

    assert len(rows) == 1
    assert rows[0].stage == "Initial sent"
    assert rows[0].days_since == 2  # Tue 2026-08-25 and Wed 2026-08-26


def test_dashboard_rows_shows_only_latest_row_per_contact(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _, contact_id = _seed_email_queue_row(
            conn, followup_number=0, sent_at="2026-08-17T09:00:00"
        )
        conn.execute(
            "INSERT INTO email_queue (contact_id, company_id, niche, subject, body, kind, "
            "followup_number, attached_resume, status, sent_at) "
            "SELECT contact_id, company_id, niche, subject, body, 'followup', 1, 0, 'sent', "
            "'2026-08-24T09:00:00' FROM email_queue WHERE contact_id = ?",
            (contact_id,),
        )
        conn.commit()

        rows = dashboard_rows(conn, today=date(2026, 8, 26))

    assert len(rows) == 1
    assert rows[0].stage == "Follow-up 1 sent"


def test_dashboard_rows_labels_terminal_statuses(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _seed_email_queue_row(conn, status="replied", sent_at="2026-08-17T09:00:00")

        rows = dashboard_rows(conn)

    assert rows[0].stage == "Replied"


def test_dashboard_rows_surfaces_company_outcome(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        company_id, _ = _seed_email_queue_row(conn, sent_at="2026-08-17T09:00:00")
        set_company_outcome(conn, company_id, "got_referral", "Met at a meetup")

        rows = dashboard_rows(conn)

    assert rows[0].outcome_status == "got_referral"
    assert rows[0].outcome_notes == "Met at a meetup"


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_counts_current_stage_per_contact(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _seed_email_queue_row(conn, status="sent", sent_at="2026-08-24T09:00:00")
        _seed_email_queue_row(conn, status="replied", sent_at="2026-08-17T09:00:00")
        _seed_email_queue_row(conn, status="no_response", sent_at="2026-08-01T09:00:00")
        _seed_email_queue_row(conn, status="bounced", sent_at="2026-08-24T09:00:00")
        _seed_email_queue_row(conn, status="approved")

        summary = summarize(conn)

    assert summary.sent == 1
    assert summary.replied == 1
    assert summary.no_response == 1
    assert summary.bounced == 1
    assert summary.awaiting_send == 1


def test_summarize_does_not_double_count_contact_with_old_and_new_rows(settings):
    """A contact with an old 'sent' initial and a newer 'approved' follow-up queued
    behind it should count once, under its current (latest) stage only.
    """
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _, contact_id = _seed_email_queue_row(
            conn, followup_number=0, status="sent", sent_at="2026-08-01T09:00:00"
        )
        conn.execute(
            "INSERT INTO email_queue (contact_id, company_id, niche, subject, body, kind, "
            "followup_number, attached_resume, status) "
            "SELECT contact_id, company_id, niche, subject, body, 'followup', 1, 0, 'approved' "
            "FROM email_queue WHERE contact_id = ?",
            (contact_id,),
        )
        conn.commit()

        summary = summarize(conn)

    assert summary.sent == 0
    assert summary.awaiting_send == 1


# ---------------------------------------------------------------------------
# set_company_outcome
# ---------------------------------------------------------------------------


def test_set_company_outcome_persists(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name, source, status) VALUES ('Meesho', 'manual_csv', 'classified')"
        ).lastrowid
        conn.commit()

        set_company_outcome(conn, company_id, "interview", "Phone screen next week")

        row = conn.execute(
            "SELECT outcome_status, outcome_notes FROM companies WHERE id = ?", (company_id,)
        ).fetchone()

    assert row["outcome_status"] == "interview"
    assert row["outcome_notes"] == "Phone screen next week"


def test_set_company_outcome_can_clear_to_none(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name, source, status) VALUES ('Meesho', 'manual_csv', 'classified')"
        ).lastrowid
        conn.commit()
        set_company_outcome(conn, company_id, "rejected", "Note")

        set_company_outcome(conn, company_id, None, None)

        row = conn.execute(
            "SELECT outcome_status, outcome_notes FROM companies WHERE id = ?", (company_id,)
        ).fetchone()

    assert row["outcome_status"] is None
    assert row["outcome_notes"] is None


def test_set_company_outcome_rejects_invalid_status(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name, source, status) VALUES ('Meesho', 'manual_csv', 'classified')"
        ).lastrowid
        conn.commit()

        with pytest.raises(TrackingError, match="invalid outcome_status"):
            set_company_outcome(conn, company_id, "not_a_real_status", None)
