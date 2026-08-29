"""Reply + bounce detection for the cold-email job agent. See PROJECT.md §4.7.

Pure orchestration against a ReplyChecker interface (agent/gmail_client.ReplyChecker /
GmailReplyChecker) -- same EmailSender/EmailFinder-style split as agent/sending.py, so
this module is fully testable with a fake checker and has no dependency on
google-api-python-client itself.

A real Gmail reply is the only thing that ever sets status='replied' -- unlike the
friend's spreadsheet this project was reverse-engineered from, where a "Replied" flag
actually just meant "follow-up sequence finished" (see PROJECT.md's module-8 notes).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from agent.gmail_client import ReplyChecker


@dataclass(frozen=True)
class TrackingStats:
    replied_contacts: int = 0
    bounced_emails: int = 0


def check_replies(conn: sqlite3.Connection, checker: ReplyChecker, sender_email: str) -> int:
    """Checks every contact's active thread for a reply. On a real reply, marks ALL of
    that contact's 'sent' rows 'replied' -- pulls them out of follow-up consideration
    for good (agent/followups.py only acts on status='sent' rows).
    """
    rows = conn.execute(
        "SELECT DISTINCT contact_id, gmail_thread_id FROM email_queue "
        "WHERE status = 'sent' AND gmail_thread_id IS NOT NULL"
    ).fetchall()

    replied_contacts = 0
    for row in rows:
        if checker.thread_has_reply(row["gmail_thread_id"], sender_email):
            cursor = conn.execute(
                "UPDATE email_queue SET status = 'replied' "
                "WHERE contact_id = ? AND status = 'sent'",
                (row["contact_id"],),
            )
            if cursor.rowcount > 0:
                replied_contacts += 1

    conn.commit()
    return replied_contacts


def _increment_bounce_count(conn: sqlite3.Connection, sent_date: date) -> None:
    """Feeds the circuit breaker in agent/sending.py, which reads send_log.bounce_count
    for the most recent day with sends -- previously always zero, since nothing wrote
    to it until this module existed.
    """
    conn.execute(
        """
        INSERT INTO send_log (date, sent_count, bounce_count) VALUES (?, 0, 1)
        ON CONFLICT(date) DO UPDATE SET bounce_count = bounce_count + 1
        """,
        (sent_date.isoformat(),),
    )


def check_bounces(conn: sqlite3.Connection, checker: ReplyChecker) -> int:
    """Checks every still-'sent' email for a delivery-failure notification. Bounce mail
    doesn't land in the original thread (it's a new message to the sender's own inbox),
    so this is a separate per-recipient inbox search, not a thread check.
    """
    rows = conn.execute(
        """
        SELECT eq.id, eq.sent_at, ct.email AS to_email
        FROM email_queue eq
        JOIN contacts ct ON ct.id = eq.contact_id
        WHERE eq.status = 'sent' AND eq.sent_at IS NOT NULL
        """
    ).fetchall()

    bounced = 0
    for row in rows:
        sent_date = datetime.fromisoformat(row["sent_at"]).date()
        if checker.has_bounce(row["to_email"], sent_date):
            conn.execute("UPDATE email_queue SET status = 'bounced' WHERE id = ?", (row["id"],))
            _increment_bounce_count(conn, sent_date)
            bounced += 1

    conn.commit()
    return bounced
