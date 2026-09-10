"""Scheduled sending for the cold-email job agent. See PROJECT.md §4.6.

EmailSender is a thin interface (same pattern as CompanySource/EmailFinder) so the
Gmail-specific implementation (agent/gmail_client.py) stays out of this module — this
file has no dependency on google-api-python-client and is fully testable with a fake
sender. Everything here is pure orchestration: warm-up ramp, the pre-flight bounce-rate
circuit breaker, daily-cap accounting, and marking rows sent.
"""

from __future__ import annotations

import os
import random
import sqlite3
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterator


class SendingError(Exception):
    """Raised when a send attempt or Gmail setup step fails."""


class SendAlreadyInProgressError(SendingError):
    """Raised when another send_approved_emails run holds the lock.

    2026-09-10: Arjun clicked the dashboard's "Send Now" button multiple times while
    a slow batch was mid-flight (each send has a deliberate delay between messages, see
    min_delay_seconds/max_delay_seconds). Streamlit doesn't disable a button while its
    handler is running, so this produced two concurrent send_approved_emails calls
    against the same SQLite file: both SELECTed the same still-'approved' rows before
    either had committed a 'sent' status, so several contacts (e.g. vaibhav.haseja@
    viacom18.com) were emailed for real more than once even though each row only shows
    one 'sent' UPDATE in email_queue. A file lock closes this window at the source --
    both the manual button and the 10:30am Task Scheduler run go through this same
    function, so locking here (not just disabling the Streamlit button) also protects
    against a manual run overlapping the scheduled one.
    """


# A real send batch can legitimately run for several minutes (one row per
# min_delay_seconds..max_delay_seconds gap), but never anywhere near this long -- past
# this age a lock file must be left over from a crashed/killed process, not an active
# run, so it's safe to reclaim rather than block forever.
_LOCK_STALE_SECONDS = 30 * 60


@contextmanager
def _send_lock(lock_path: Path) -> Iterator[None]:
    """Filesystem mutex so only one send_approved_emails runs at a time.

    Uses O_CREAT|O_EXCL for an atomic create-or-fail, which is safe across separate
    processes (unlike a session_state flag, which only protects one Streamlit session
    and does nothing for the Task Scheduler process).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age < _LOCK_STALE_SECONDS:
            raise SendAlreadyInProgressError(
                "A send is already in progress (lock held for "
                f"{int(age)}s) -- refusing to start a second one. Wait for it to "
                "finish, or check data/daily_run.log / the dashboard if this seems "
                "stuck."
            )
        lock_path.unlink()  # stale lock from a crashed/killed run

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SendAlreadyInProgressError(
            "A send is already in progress -- refusing to start a second one."
        )
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


class EmailSender(ABC):
    """A pluggable email-sending backend. Gmail is the only implementation today."""

    @abstractmethod
    def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        resume_path: Path | None,
        thread_id: str | None = None,
    ) -> str:
        """Sends one email and returns a thread id for reply-tracking (§4.7).

        thread_id, when given, threads the send onto an existing Gmail conversation --
        used for follow-ups (agent/followups.py) so they land in the same thread as the
        contact's initial email instead of starting a new one.
        """


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


def is_weekend(day: date) -> bool:
    """Saturday/Sunday check (Arjun's decision, 2026-09-06) — no sends should leave the
    outbox on a non-working day, whether approved just now or days ago.
    """
    return day.weekday() >= 5


@dataclass(frozen=True)
class SendStats:
    sent: int = 0
    failed: int = 0
    circuit_breaker_tripped: bool = False
    skipped_weekend: bool = False
    daily_cap: int = 0
    remaining_capacity: int = 0


def send_approved_emails(
    conn: sqlite3.Connection,
    sender: EmailSender,
    *,
    resume_pdf_path: Path,
    bounce_rate_circuit_breaker: float,
    today: date,
    db_path: Path,
    min_delay_seconds: float = 0.0,
    max_delay_seconds: float = 0.0,
    sleep_fn: Callable[[float, float], None] = _default_sleep,
    allow_weekend: bool = False,
) -> SendStats:
    """Sends up to today's remaining daily-cap capacity of approved emails.

    Rows that fail to send stay at status='approved' (retried on the next run) rather
    than being marked bounced — a synchronous send-time failure isn't the same thing
    as an asynchronous delivery bounce (§4.7 handles those).

    Approved rows never leave the outbox on a Saturday/Sunday unless the caller passes
    allow_weekend=True (the dashboard's "Send Now" button does this only after the user
    explicitly confirms a warning) — the scheduled daily run never sets it, so an
    approval made on a Friday/Saturday/Sunday just waits at status='approved' until the
    next weekday's 10:30am run. Rows are left untouched, not rejected.

    Raises SendAlreadyInProgressError instead of sending anything if another call to
    this function (manual "Send Now" or the scheduled run, in this or another process)
    is already mid-batch — see that class's docstring for the 2026-09-10 double-send
    incident this closes.
    """
    with _send_lock(db_path.parent / "send.lock"):
        return _send_approved_emails_locked(
            conn,
            sender,
            resume_pdf_path=resume_pdf_path,
            bounce_rate_circuit_breaker=bounce_rate_circuit_breaker,
            today=today,
            min_delay_seconds=min_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            sleep_fn=sleep_fn,
            allow_weekend=allow_weekend,
        )


def _send_approved_emails_locked(
    conn: sqlite3.Connection,
    sender: EmailSender,
    *,
    resume_pdf_path: Path,
    bounce_rate_circuit_breaker: float,
    today: date,
    min_delay_seconds: float = 0.0,
    max_delay_seconds: float = 0.0,
    sleep_fn: Callable[[float, float], None] = _default_sleep,
    allow_weekend: bool = False,
) -> SendStats:
    if is_weekend(today) and not allow_weekend:
        return SendStats(skipped_weekend=True)

    if _recent_bounce_rate_exceeds_threshold(conn, bounce_rate_circuit_breaker):
        return SendStats(circuit_breaker_tripped=True)

    daily_cap = apply_ramp_if_due(conn, today)
    already_sent_today = _sent_count_for_date(conn, today)
    remaining = max(0, daily_cap - already_sent_today)

    if remaining == 0:
        return SendStats(daily_cap=daily_cap, remaining_capacity=0)

    rows = conn.execute(
        """
        SELECT eq.id, eq.subject, eq.body, eq.attached_resume, eq.gmail_thread_id,
               ct.email AS to_email
        FROM email_queue eq
        JOIN contacts ct ON ct.id = eq.contact_id
        WHERE eq.status = 'approved'
        ORDER BY eq.approved_at ASC, eq.id ASC
        LIMIT ?
        """,
        (remaining,),
    ).fetchall()

    sent = 0
    failed = 0
    for i, row in enumerate(rows):
        resume_path = resume_pdf_path if row["attached_resume"] else None
        try:
            thread_id = sender.send(
                row["to_email"],
                row["subject"],
                row["body"],
                resume_path,
                thread_id=row["gmail_thread_id"],
            )
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
