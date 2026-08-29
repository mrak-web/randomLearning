from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db
from agent.followups import business_days_since, generate_due_followups

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


# ---------------------------------------------------------------------------
# business_days_since
# ---------------------------------------------------------------------------


def test_business_days_since_same_day_is_zero():
    assert business_days_since("2026-08-24T09:00:00", date(2026, 8, 24)) == 0


def test_business_days_since_counts_weekdays_only():
    # 2026-08-24 is a Monday; +5 calendar days lands on Saturday 2026-08-29, which is
    # only 4 weekdays later (Tue/Wed/Thu/Fri).
    assert business_days_since("2026-08-24T09:00:00", date(2026, 8, 29)) == 4


def test_business_days_since_spans_a_weekend():
    # Friday 2026-08-21 -> Monday 2026-08-24 is 1 business day, despite 3 calendar days.
    assert business_days_since("2026-08-21T09:00:00", date(2026, 8, 24)) == 1


# ---------------------------------------------------------------------------
# generate_due_followups (integration against a real temp DB)
# ---------------------------------------------------------------------------


def _insert_sent_email(
    conn, *, company_name="Meesho", contact_email="priya@meesho.example",
    followup_number=0, sent_at, gmail_thread_id="thread-1",
) -> tuple[int, int]:
    company_id = conn.execute(
        "INSERT INTO companies (name, source, niche, status) VALUES (?, 'manual_csv', 'consumer', 'classified')",
        (company_name,),
    ).lastrowid
    contact_id = conn.execute(
        "INSERT INTO contacts (company_id, name, email, role_category, status) "
        "VALUES (?, ?, ?, 'hr', 'verified')",
        (company_id, "Priya Sharma", contact_email),
    ).lastrowid
    conn.execute(
        "INSERT INTO email_queue (contact_id, company_id, niche, subject, body, kind, "
        "followup_number, attached_resume, status, sent_at, gmail_thread_id) "
        "VALUES (?, ?, 'consumer', 'Subject', 'Body', 'initial', ?, 1, 'sent', ?, ?)",
        (contact_id, company_id, followup_number, sent_at, gmail_thread_id),
    )
    conn.commit()
    return company_id, contact_id


def test_generate_due_followups_generates_first_round_when_due(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, sent_at="2026-08-17T09:00:00", gmail_thread_id="thread-abc")

        stats = generate_due_followups(
            conn,
            templates_dir=settings.templates_dir,
            sender_display_name="Arjun Khanna",
            business_days_wait=5,
            max_followups=2,
            sender_phone="+91 9466898689",
            today=date(2026, 8, 24),  # 5 business days after 2026-08-17 (Monday)
        )

        row = conn.execute(
            "SELECT * FROM email_queue WHERE followup_number = 1"
        ).fetchone()

    assert stats.generated == 1
    assert stats.retired_no_response == 0
    assert row is not None
    assert row["kind"] == "followup"
    assert row["status"] == "approved"  # auto-sends, no review step
    assert row["gmail_thread_id"] == "thread-abc"  # threaded onto the original
    assert row["attached_resume"] == 0
    assert "Meesho" in row["subject"]
    assert "+91 9466898689" in row["body"]


def test_generate_due_followups_not_yet_due_generates_nothing(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, sent_at="2026-08-21T09:00:00")  # Friday

        stats = generate_due_followups(
            conn,
            templates_dir=settings.templates_dir,
            sender_display_name="Arjun Khanna",
            business_days_wait=5,
            max_followups=2,
            today=date(2026, 8, 24),  # only 1 business day later
        )

    assert stats.generated == 0
    assert stats.retired_no_response == 0


def test_generate_due_followups_is_safe_to_rerun_before_send(settings):
    """A newly auto-approved follow-up row is itself the 'latest row' for its contact,
    so re-running before send_batch.py picks it up must not generate a duplicate.
    """
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, sent_at="2026-08-17T09:00:00")

        kwargs = dict(
            templates_dir=settings.templates_dir,
            sender_display_name="Arjun Khanna",
            business_days_wait=5,
            max_followups=2,
            today=date(2026, 8, 24),
        )
        first = generate_due_followups(conn, **kwargs)
        second = generate_due_followups(conn, **kwargs)

        count = conn.execute("SELECT COUNT(*) FROM email_queue").fetchone()[0]

    assert first.generated == 1
    assert second.generated == 0
    assert count == 2  # initial + one follow-up, not two follow-ups


def test_generate_due_followups_second_round_after_first_is_sent(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(
            conn, followup_number=1, sent_at="2026-08-17T09:00:00", gmail_thread_id="thread-xyz"
        )

        stats = generate_due_followups(
            conn,
            templates_dir=settings.templates_dir,
            sender_display_name="Arjun Khanna",
            business_days_wait=5,
            max_followups=2,
            today=date(2026, 8, 24),
        )

        row = conn.execute("SELECT * FROM email_queue WHERE followup_number = 2").fetchone()

    assert stats.generated == 1
    assert row["gmail_thread_id"] == "thread-xyz"


def test_generate_due_followups_retires_after_max_reached(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        company_id, contact_id = _insert_sent_email(
            conn, followup_number=2, sent_at="2026-08-17T09:00:00"
        )

        stats = generate_due_followups(
            conn,
            templates_dir=settings.templates_dir,
            sender_display_name="Arjun Khanna",
            business_days_wait=5,
            max_followups=2,
            today=date(2026, 8, 24),
        )

        row = conn.execute(
            "SELECT status FROM email_queue WHERE contact_id = ?", (contact_id,)
        ).fetchone()

    assert stats.generated == 0
    assert stats.retired_no_response == 1
    assert row["status"] == "no_response"


def test_generate_due_followups_ignores_replied_contacts(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, sent_at="2026-08-17T09:00:00")
        conn.execute("UPDATE email_queue SET status = 'replied'")
        conn.commit()

        stats = generate_due_followups(
            conn,
            templates_dir=settings.templates_dir,
            sender_display_name="Arjun Khanna",
            business_days_wait=5,
            max_followups=2,
            today=date(2026, 8, 24),
        )

    assert stats.generated == 0
    assert stats.retired_no_response == 0
