from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db
from agent.sending import (
    EmailSender,
    SendingError,
    apply_ramp_if_due,
    send_approved_emails,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_RESUME = REPO_ROOT / "tests" / "fixtures" / "fake_resume.pdf"


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


class FakeEmailSender(EmailSender):
    def __init__(self, responses: dict | None = None, default_thread_id: str = "thread-1"):
        self.responses = responses or {}
        self.default_thread_id = default_thread_id
        self.calls: list[tuple] = []

    def send(self, to_email, subject, body, resume_path, thread_id=None):
        self.calls.append((to_email, subject, body, resume_path, thread_id))
        result = self.responses.get(to_email, self.default_thread_id)
        if isinstance(result, Exception):
            raise result
        return result


def _fake_sleep_recorder():
    calls = []

    def sleep_fn(min_seconds, max_seconds):
        calls.append((min_seconds, max_seconds))

    return sleep_fn, calls


def _insert_approved_email(
    conn, *, company_name, contact_email, attached_resume=1, created_at=None, gmail_thread_id=None
) -> int:
    company_id = conn.execute(
        "INSERT INTO companies (name, source, niche, status) VALUES (?, 'manual_csv', 'consumer', 'classified')",
        (company_name,),
    ).lastrowid
    contact_id = conn.execute(
        "INSERT INTO contacts (company_id, name, email, role_category, status) "
        "VALUES (?, ?, ?, 'hr', 'verified')",
        (company_id, "Contact Person", contact_email),
    ).lastrowid
    if created_at is not None:
        eq_id = conn.execute(
            "INSERT INTO email_queue (contact_id, company_id, niche, subject, body, kind, "
            "attached_resume, status, created_at, gmail_thread_id) VALUES (?, ?, 'consumer', ?, ?, "
            "'initial', ?, 'approved', ?, ?)",
            (
                contact_id, company_id, f"Subject for {company_name}", "Body text", attached_resume,
                created_at, gmail_thread_id,
            ),
        ).lastrowid
    else:
        eq_id = conn.execute(
            "INSERT INTO email_queue (contact_id, company_id, niche, subject, body, kind, "
            "attached_resume, status, gmail_thread_id) VALUES (?, ?, 'consumer', ?, ?, 'initial', ?, "
            "'approved', ?)",
            (contact_id, company_id, f"Subject for {company_name}", "Body text", attached_resume, gmail_thread_id),
        ).lastrowid
    conn.commit()
    return eq_id


# ---------------------------------------------------------------------------
# apply_ramp_if_due
# ---------------------------------------------------------------------------


def test_apply_ramp_first_call_sets_baseline_without_ramping(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        cap = apply_ramp_if_due(conn, date(2026, 1, 1))
        row = conn.execute("SELECT daily_cap, last_ramp_date FROM send_config WHERE id = 1").fetchone()

    assert cap == settings.send.start_daily_cap
    assert row["last_ramp_date"] == "2026-01-01"
    assert row["daily_cap"] == settings.send.start_daily_cap


def test_apply_ramp_same_day_is_a_noop(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        apply_ramp_if_due(conn, date(2026, 1, 1))
        cap = apply_ramp_if_due(conn, date(2026, 1, 1))

    assert cap == settings.send.start_daily_cap


def test_apply_ramp_before_interval_elapsed_is_a_noop(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        apply_ramp_if_due(conn, date(2026, 1, 1))
        cap = apply_ramp_if_due(conn, date(2026, 1, 1) + timedelta(days=1))

    assert cap == settings.send.start_daily_cap


def test_apply_ramp_after_interval_increments_cap(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        apply_ramp_if_due(conn, date(2026, 1, 1))
        cap = apply_ramp_if_due(
            conn, date(2026, 1, 1) + timedelta(days=settings.send.ramp_interval_days)
        )

    assert cap == settings.send.start_daily_cap + settings.send.ramp_step


def test_apply_ramp_caps_at_ceiling(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute(
            "UPDATE send_config SET daily_cap = ?, last_ramp_date = '2026-01-01' WHERE id = 1",
            (settings.send.ramp_ceiling - 1,),
        )
        conn.commit()

        cap = apply_ramp_if_due(
            conn, date(2026, 1, 1) + timedelta(days=settings.send.ramp_interval_days)
        )

    assert cap == settings.send.ramp_ceiling


def test_apply_ramp_stays_at_ceiling(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute(
            "UPDATE send_config SET daily_cap = ?, last_ramp_date = '2026-01-01' WHERE id = 1",
            (settings.send.ramp_ceiling,),
        )
        conn.commit()

        cap = apply_ramp_if_due(
            conn, date(2026, 1, 1) + timedelta(days=2 * settings.send.ramp_interval_days)
        )

    assert cap == settings.send.ramp_ceiling


# ---------------------------------------------------------------------------
# send_approved_emails
# ---------------------------------------------------------------------------


def test_send_approved_emails_sends_and_marks_sent(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")
        sender = FakeEmailSender(default_thread_id="thread-abc")

        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

        row = conn.execute("SELECT status, gmail_thread_id, sent_at FROM email_queue").fetchone()
        log_row = conn.execute("SELECT sent_count, bounce_count FROM send_log WHERE date = '2026-01-01'").fetchone()

    assert stats.sent == 1
    assert stats.failed == 0
    assert row["status"] == "sent"
    assert row["gmail_thread_id"] == "thread-abc"
    assert row["sent_at"] is not None
    assert log_row["sent_count"] == 1
    assert log_row["bounce_count"] == 0
    assert sender.calls == [("priya@meesho.example", "Subject for Meesho", "Body text", FAKE_RESUME, None)]


def test_send_approved_emails_passes_none_when_resume_not_attached(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_approved_email(
            conn, company_name="Meesho", contact_email="priya@meesho.example", attached_resume=0
        )
        sender = FakeEmailSender()

        send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

    assert sender.calls[0][3] is None


def test_send_approved_emails_respects_daily_cap(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute("UPDATE send_config SET daily_cap = 2 WHERE id = 1")
        conn.commit()
        for i in range(5):
            _insert_approved_email(conn, company_name=f"Co{i}", contact_email=f"c{i}@example.com")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

        remaining_approved = conn.execute(
            "SELECT COUNT(*) FROM email_queue WHERE status = 'approved'"
        ).fetchone()[0]

    assert stats.sent == 2
    assert remaining_approved == 3


def test_send_approved_emails_accounts_for_already_sent_today(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute("UPDATE send_config SET daily_cap = 3, last_ramp_date = '2026-01-01' WHERE id = 1")
        conn.execute(
            "INSERT INTO send_log (date, sent_count, bounce_count) VALUES ('2026-01-01', 2, 0)"
        )
        conn.commit()
        for i in range(5):
            _insert_approved_email(conn, company_name=f"Co{i}", contact_email=f"c{i}@example.com")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

        log_row_count = conn.execute(
            "SELECT sent_count FROM send_log WHERE date = '2026-01-01'"
        ).fetchone()

    assert stats.sent == 1  # cap 3 minus 2 already sent today
    assert log_row_count["sent_count"] == 3


def test_send_approved_emails_no_remaining_capacity_sends_nothing(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute("UPDATE send_config SET daily_cap = 2, last_ramp_date = '2026-01-01' WHERE id = 1")
        conn.execute(
            "INSERT INTO send_log (date, sent_count, bounce_count) VALUES ('2026-01-01', 2, 0)"
        )
        conn.commit()
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

    assert stats.sent == 0
    assert stats.remaining_capacity == 0
    assert sender.calls == []


def test_send_approved_emails_circuit_breaker_trips_and_sends_nothing(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO send_log (date, sent_count, bounce_count) VALUES ('2025-12-31', 10, 2)"
        )
        conn.commit()
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=0.05,
            today=date(2026, 1, 1),
        )

        row = conn.execute("SELECT status FROM email_queue").fetchone()

    assert stats.circuit_breaker_tripped is True
    assert stats.sent == 0
    assert sender.calls == []
    assert row["status"] == "approved"


def test_send_approved_emails_circuit_breaker_not_tripped_below_threshold(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO send_log (date, sent_count, bounce_count) VALUES ('2025-12-31', 100, 1)"
        )
        conn.commit()
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=0.05,
            today=date(2026, 1, 1),
        )

    assert stats.circuit_breaker_tripped is False
    assert stats.sent == 1


def test_send_approved_emails_send_failure_leaves_row_approved(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_approved_email(conn, company_name="Broken Co", contact_email="broken@example.com")
        _insert_approved_email(conn, company_name="Fine Co", contact_email="fine@example.com")

        sender = FakeEmailSender(
            responses={"broken@example.com": SendingError("Gmail 500")}
        )
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

        statuses = {
            row["subject"]: row["status"]
            for row in conn.execute("SELECT subject, status FROM email_queue").fetchall()
        }

    assert stats.sent == 1
    assert stats.failed == 1
    assert statuses["Subject for Broken Co"] == "approved"
    assert statuses["Subject for Fine Co"] == "sent"


def test_send_approved_emails_sleeps_between_but_not_after_last(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        for i in range(3):
            _insert_approved_email(conn, company_name=f"Co{i}", contact_email=f"c{i}@example.com")

        sleep_fn, sleep_calls = _fake_sleep_recorder()
        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
            min_delay_seconds=1,
            max_delay_seconds=2,
            sleep_fn=sleep_fn,
        )

    assert stats.sent == 3
    assert len(sleep_calls) == 2
    assert all(call == (1, 2) for call in sleep_calls)


def test_send_approved_emails_no_sleep_when_max_delay_zero(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        for i in range(3):
            _insert_approved_email(conn, company_name=f"Co{i}", contact_email=f"c{i}@example.com")

        sleep_fn, sleep_calls = _fake_sleep_recorder()
        sender = FakeEmailSender()
        send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
            sleep_fn=sleep_fn,
        )

    assert sleep_calls == []


def test_send_approved_emails_passes_existing_thread_id_for_followups(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_approved_email(
            conn,
            company_name="Meesho",
            contact_email="priya@meesho.example",
            gmail_thread_id="thread-original",
        )
        sender = FakeEmailSender()

        send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

    assert sender.calls[0][4] == "thread-original"


def test_send_approved_emails_no_approved_rows_is_clean(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
        )

    assert stats.sent == 0
    assert stats.failed == 0
    assert sender.calls == []
