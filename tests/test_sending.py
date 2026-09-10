from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db
from agent.sending import (
    EmailSender,
    SendAlreadyInProgressError,
    SendingError,
    apply_ramp_if_due,
    is_weekend,
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
    conn,
    *,
    company_name,
    contact_email,
    attached_resume=1,
    created_at=None,
    approved_at=None,
    gmail_thread_id=None,
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
    columns = ["contact_id", "company_id", "niche", "subject", "body", "kind", "attached_resume", "status", "gmail_thread_id"]
    values = [
        contact_id, company_id, "consumer", f"Subject for {company_name}", "Body text", "initial",
        attached_resume, "approved", gmail_thread_id,
    ]
    if created_at is not None:
        columns.append("created_at")
        values.append(created_at)
    if approved_at is not None:
        columns.append("approved_at")
        values.append(approved_at)
    placeholders = ", ".join("?" for _ in values)
    eq_id = conn.execute(
        f"INSERT INTO email_queue ({', '.join(columns)}) VALUES ({placeholders})", values
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
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
            db_path=settings.db_path,
        )

    assert sender.calls[0][4] == "thread-original"


def test_send_approved_emails_orders_by_approval_time_not_creation_time(settings):
    """A draft generated earlier but approved later should send *after* one generated
    later but approved first -- Arjun's actual approval order, not generation order
    (2026-09-09)."""
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_approved_email(
            conn,
            company_name="GeneratedFirst",
            contact_email="first-generated@example.com",
            created_at="2026-01-01T00:00:00",
            approved_at="2026-01-05T00:00:00",  # approved last
        )
        _insert_approved_email(
            conn,
            company_name="GeneratedSecond",
            contact_email="second-generated@example.com",
            created_at="2026-01-02T00:00:00",
            approved_at="2026-01-03T00:00:00",  # approved first
        )

        sender = FakeEmailSender()
        send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 6),  # a Tuesday -- avoid the weekend send guardrail
            db_path=settings.db_path,
        )

    sent_order = [call[0] for call in sender.calls]
    assert sent_order == ["second-generated@example.com", "first-generated@example.com"]


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
            db_path=settings.db_path,
        )

    assert stats.sent == 0
    assert stats.failed == 0
    assert sender.calls == []


# ---------------------------------------------------------------------------
# weekend guardrail (Arjun's decision, 2026-09-06: nothing sends Sat/Sun unless
# explicitly overridden)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("day", [date(2026, 1, 3), date(2026, 1, 4)])  # Sat, Sun
def test_is_weekend_true_for_saturday_and_sunday(day):
    assert is_weekend(day) is True


@pytest.mark.parametrize("day", [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 5)])  # Thu, Fri, Mon
def test_is_weekend_false_for_weekdays(day):
    assert is_weekend(day) is False


@pytest.mark.parametrize("weekend_day", [date(2026, 1, 3), date(2026, 1, 4)])  # Sat, Sun
def test_send_approved_emails_skips_on_weekend_by_default(settings, weekend_day):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=weekend_day,
            db_path=settings.db_path,
        )

        row = conn.execute("SELECT status FROM email_queue").fetchone()

    assert stats.skipped_weekend is True
    assert stats.sent == 0
    assert sender.calls == []
    assert row["status"] == "approved"  # untouched, waits for the next weekday run


def test_send_approved_emails_sends_on_weekend_when_explicitly_allowed(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 3),  # Saturday
            db_path=settings.db_path,
            allow_weekend=True,
        )

        row = conn.execute("SELECT status FROM email_queue").fetchone()

    assert stats.skipped_weekend is False
    assert stats.sent == 1
    assert row["status"] == "sent"


# ---------------------------------------------------------------------------
# concurrent-send lock (2026-09-10 double-send incident: two "Send Now" clicks
# raced each other and emailed several contacts, e.g. vaibhav.haseja@viacom18.com,
# twice for real even though email_queue only shows one 'sent' row each)
# ---------------------------------------------------------------------------


def test_send_approved_emails_rejects_concurrent_call(settings):
    init_db(settings.db_path, settings)
    lock_path = settings.db_path.parent / "send.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("12345")

    with connect(settings.db_path) as conn:
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")
        sender = FakeEmailSender()

        with pytest.raises(SendAlreadyInProgressError):
            send_approved_emails(
                conn,
                sender,
                resume_pdf_path=FAKE_RESUME,
                bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
                today=date(2026, 1, 1),
                db_path=settings.db_path,
            )

        row = conn.execute("SELECT status FROM email_queue").fetchone()

    assert sender.calls == []
    assert row["status"] == "approved"  # never touched -- the lock blocked the send entirely


def test_send_approved_emails_reclaims_stale_lock(settings, monkeypatch):
    init_db(settings.db_path, settings)
    lock_path = settings.db_path.parent / "send.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("12345")

    import os
    import time as time_module

    stale_time = time_module.time() - 3600  # older than _LOCK_STALE_SECONDS
    os.utime(lock_path, (stale_time, stale_time))

    with connect(settings.db_path) as conn:
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")
        sender = FakeEmailSender()

        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
            db_path=settings.db_path,
        )

    assert stats.sent == 1
    assert not lock_path.exists()  # released after the (reclaimed) run finished


def test_send_approved_emails_releases_lock_after_success(settings):
    init_db(settings.db_path, settings)
    lock_path = settings.db_path.parent / "send.lock"

    with connect(settings.db_path) as conn:
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")
        sender = FakeEmailSender()

        send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date(2026, 1, 1),
            db_path=settings.db_path,
        )

    assert not lock_path.exists()


def test_send_approved_emails_releases_lock_when_circuit_breaker_trips(settings):
    """The lock must not leak on any early-return path, not just the happy path."""
    init_db(settings.db_path, settings)
    lock_path = settings.db_path.parent / "send.lock"

    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO send_log (date, sent_count, bounce_count) VALUES ('2025-12-31', 10, 10)"
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
            db_path=settings.db_path,
        )

    assert stats.circuit_breaker_tripped is True
    assert not lock_path.exists()


def test_send_approved_emails_weekend_skip_checked_before_circuit_breaker(settings):
    """The weekend gate is a hard stop -- it should short-circuit even when the
    circuit breaker would also have blocked the send, so the reported reason for
    nothing going out is unambiguous."""
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO send_log (date, sent_count, bounce_count) VALUES ('2025-12-31', 10, 10)"
        )
        conn.commit()
        _insert_approved_email(conn, company_name="Meesho", contact_email="priya@meesho.example")

        sender = FakeEmailSender()
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=FAKE_RESUME,
            bounce_rate_circuit_breaker=0.05,
            today=date(2026, 1, 3),  # Saturday
            db_path=settings.db_path,
        )

    assert stats.skipped_weekend is True
    assert stats.circuit_breaker_tripped is False
