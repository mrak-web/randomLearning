"""Scheduled sending for the cold-email job agent. See PROJECT.md §4.6.

EmailSender is a thin interface (same pattern as CompanySource/EmailFinder) so the
Gmail-specific implementation (agent/gmail_client.py) stays out of this module — this
file has no dependency on google-api-python-client and is fully testable with a fake
sender. Everything here is pure orchestration: warm-up ramp, the pre-flight bounce-rate
circuit breaker, daily-cap accounting, and marking rows sent.
"""

from __future__ import annotations

import random
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable


class SendingError(Exception):
    """Raised when a send attempt or Gmail setup step fails."""


class EmailSender(ABC):
    """A pluggable email-sending backend. Gmail is the only implementation today."""

    @abstractmethod
    def send(
        self, to_email: str, subject: str, body: str, resume_path: Path | None
    ) -> str:
        """Sends one email and returns a thread id for reply-tracking (§4.7)."""


def _default_sleep(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def apply_ramp_if_due(conn: sqlite3.Connection, today: date) -> int:
    """Bumps send_config.daily_cap by ramp_step if ramp_interval_days have elapsed.

    The first call after a fresh init_db (last_ramp_date is NULL) just establishes
    today as the baseline rather than ramping immediately — the cap only grows once
    a full interval has actually passed at the current cap.
    """
    row = conn.execute(
        "SELECT daily_cap, ramp_step, ramp_ceiling, ramp_interval_days, last_ramp_date "
        "FROM send_config WHERE id = 1"
    ).fetchone()
    if row is None:
        raise SendingError("send_config row missing — run init_db first")

    daily_cap = row["daily_cap"]
    last_ramp_date = row["last_ramp_date"]

    if last_ramp_date is None:
        conn.execute(
            "UPDATE send_config SET last_ramp_date = ? WHERE id = 1", (today.isoformat(),)
        )
        conn.commit()
        return daily_cap

    days_elapsed = (today - date.fromisoformat(last_ramp_date)).days
    if days_elapsed >= row["ramp_interval_days"] and daily_cap < row["ramp_ceiling"]:
        new_cap = min(daily_cap + row["ramp_step"], row["ramp_ceiling"])
        conn.execute(
            "UPDATE send_config SET daily_cap = ?, last_ramp_date = ? WHERE id = 1",
            (new_cap, today.isoformat()),
        )
        conn.commit()
        return new_cap

    return daily_cap


def _sent_count_for_date(conn: sqlite3.Connection, day: date) -> int:
    row = conn.execute(
        "SELECT sent_count FROM send_log WHERE date = ?", (day.isoformat(),)
    ).fetchone()
    return row["sent_count"] if row else 0


def _record_sent_count(conn: sqlite3.Connection, day: date, count: int) -> None:
    if count == 0:
        return
    conn.execute(
        """
        INSERT INTO send_log (date, sent_count, bounce_count) VALUES (?, ?, 0)
        ON CONFLICT(date) DO UPDATE SET sent_count = sent_count + excluded.sent_count
        """,
        (day.isoformat(), count),
    )
    conn.commit()


def _recent_bounce_rate_exceeds_threshold(conn: sqlite3.Connection, threshold: float) -> bool:
    """Checks the most recent day with any sends — not this run, which hasn't happened yet.

    Bounces are detected asynchronously by module 8 (tracking), so this is a pre-flight
    check against trailing data, not something computed mid-send.
    """
    row = conn.execute(
        "SELECT sent_count, bounce_count FROM send_log "
        "WHERE sent_count > 0 ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return False
    return (row["bounce_count"] / row["sent_count"]) > threshold


@dataclass(frozen=True)
class SendStats:
    sent: int = 0
    failed: int = 0
    circuit_breaker_tripped: bool = False
    daily_cap: int = 0
    remaining_capacity: int = 0


def send_approved_emails(
    conn: sqlite3.Connection,
    sender: EmailSender,
    *,
    resume_pdf_path: Path,
    bounce_rate_circuit_breaker: float,
    today: date,
    min_delay_seconds: float = 0.0,
    max_delay_seconds: float = 0.0,
    sleep_fn: Callable[[float, float], None] = _default_sleep,
) -> SendStats:
    """Sends up to today's remaining daily-cap capacity of approved emails.

    Rows that fail to send stay at status='approved' (retried on the next run) rather
    than being marked bounced — a synchronous send-time failure isn't the same thing
    as an asynchronous delivery bounce (§4.7 handles those).
    """
    if _recent_bounce_rate_exceeds_threshold(conn, bounce_rate_circuit_breaker):
        return SendStats(circuit_breaker_tripped=True)

    daily_cap = apply_ramp_if_due(conn, today)
    already_sent_today = _sent_count_for_date(conn, today)
    remaining = max(0, daily_cap - already_sent_today)

    if remaining == 0:
        return SendStats(daily_cap=daily_cap, remaining_capacity=0)

    rows = conn.execute(
        """
        SELECT eq.id, eq.subject, eq.body, eq.attached_resume, ct.email AS to_email
        FROM email_queue eq
        JOIN contacts ct ON ct.id = eq.contact_id
        WHERE eq.status = 'approved'
        ORDER BY eq.created_at ASC, eq.id ASC
        LIMIT ?
        """,
        (remaining,),
    ).fetchall()

    sent = 0
    failed = 0
    for i, row in enumerate(rows):
        resume_path = resume_pdf_path if row["attached_resume"] else None
        try:
            thread_id = sender.send(row["to_email"], row["subject"], row["body"], resume_path)
        except SendingError:
            failed += 1
            continue

        conn.execute(
            "UPDATE email_queue SET status = 'sent', gmail_thread_id = ?, sent_at = ? WHERE id = ?",
            (thread_id, datetime.now().isoformat(timespec="seconds"), row["id"]),
        )
        conn.commit()
        sent += 1

        if i < len(rows) - 1 and max_delay_seconds > 0:
            sleep_fn(min_delay_seconds, max_delay_seconds)

    _record_sent_count(conn, today, sent)

    return SendStats(
        sent=sent,
        failed=failed,
        daily_cap=daily_cap,
        remaining_capacity=remaining,
    )
