from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db
from agent.gmail_client import ReplyChecker
from agent.tracking import check_bounces, check_replies

SENDER_EMAIL = "arjun@example.com"


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db", sender_email=SENDER_EMAIL)


class FakeReplyChecker(ReplyChecker):
    def __init__(self, replied_threads: set[str] | None = None, bounced_emails: set[str] | None = None):
        self.replied_threads = replied_threads or set()
        self.bounced_emails = bounced_emails or set()

    def thread_has_reply(self, thread_id: str, sender_email: str) -> bool:
        return thread_id in self.replied_threads

    def has_bounce(self, recipient_email: str, after: date) -> bool:
        return recipient_email in self.bounced_emails


def _insert_sent_email(
    conn, *, company_name="Meesho", contact_email="priya@meesho.example",
    followup_number=0, sent_at="2026-08-24T09:00:00", gmail_thread_id="thread-1", status="sent",
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
        "VALUES (?, ?, 'consumer', 'Subject', 'Body', 'initial', ?, 1, ?, ?, ?)",
        (contact_id, company_id, followup_number, status, sent_at, gmail_thread_id),
    )
    conn.commit()
    return company_id, contact_id


# ---------------------------------------------------------------------------
# check_replies
# ---------------------------------------------------------------------------


def test_check_replies_marks_replied_when_thread_has_reply(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, gmail_thread_id="thread-replied")
        checker = FakeReplyChecker(replied_threads={"thread-replied"})

        count = check_replies(conn, checker, SENDER_EMAIL)

        row = conn.execute("SELECT status FROM email_queue").fetchone()

    assert count == 1
    assert row["status"] == "replied"


def test_check_replies_leaves_unreplied_threads_alone(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, gmail_thread_id="thread-quiet")
        checker = FakeReplyChecker(replied_threads=set())

        count = check_replies(conn, checker, SENDER_EMAIL)

        row = conn.execute("SELECT status FROM email_queue").fetchone()

    assert count == 0
    assert row["status"] == "sent"


def test_check_replies_marks_all_rows_for_a_replied_contact(settings):
    """An initial + a follow-up both share a thread once threaded -- a reply on that
    thread should retire the contact entirely, not just the row that happened to
    trigger the thread lookup.
    """
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _, contact_id = _insert_sent_email(
            conn, followup_number=0, sent_at="2026-08-17T09:00:00", gmail_thread_id="thread-shared"
        )
        conn.execute(
            "INSERT INTO email_queue (contact_id, company_id, niche, subject, body, kind, "
            "followup_number, attached_resume, status, sent_at, gmail_thread_id) "
            "SELECT contact_id, company_id, niche, subject, body, 'followup', 1, 0, 'sent', "
            "'2026-08-24T09:00:00', gmail_thread_id FROM email_queue WHERE contact_id = ?",
            (contact_id,),
        )
        conn.commit()
        checker = FakeReplyChecker(replied_threads={"thread-shared"})

        check_replies(conn, checker, SENDER_EMAIL)

        statuses = {
            row["followup_number"]: row["status"]
            for row in conn.execute("SELECT followup_number, status FROM email_queue").fetchall()
        }

    assert statuses == {0: "replied", 1: "replied"}


# ---------------------------------------------------------------------------
# check_bounces
# ---------------------------------------------------------------------------


def test_check_bounces_marks_bounced_and_updates_send_log(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(
            conn, contact_email="ghost@nowhere.example", sent_at="2026-08-24T09:00:00"
        )
        checker = FakeReplyChecker(bounced_emails={"ghost@nowhere.example"})

        count = check_bounces(conn, checker)

        row = conn.execute("SELECT status FROM email_queue").fetchone()
        log_row = conn.execute(
            "SELECT sent_count, bounce_count FROM send_log WHERE date = '2026-08-24'"
        ).fetchone()

    assert count == 1
    assert row["status"] == "bounced"
    assert log_row["bounce_count"] == 1
    assert log_row["sent_count"] == 0  # send_log's sent_count is tracked by sending.py separately


def test_check_bounces_ignores_delivered_emails(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, contact_email="priya@meesho.example")
        checker = FakeReplyChecker(bounced_emails=set())

        count = check_bounces(conn, checker)

        row = conn.execute("SELECT status FROM email_queue").fetchone()

    assert count == 0
    assert row["status"] == "sent"


def test_check_bounces_only_checks_sent_rows(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _insert_sent_email(conn, contact_email="already@replied.example", status="replied")
        checker = FakeReplyChecker(bounced_emails={"already@replied.example"})

        count = check_bounces(conn, checker)

    assert count == 0
