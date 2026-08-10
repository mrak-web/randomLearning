"""Send approved emails for today's batch via Gmail. See PROJECT.md §4.6.

Usage: py scripts/send_batch.py

Picks up to the current daily cap (after applying any due warm-up ramp) of
status='approved' rows, sends via Gmail, marks them 'sent' with the Gmail thread_id.
Halts before sending anything if the most recent day with sends had a bounce rate over
the circuit-breaker threshold. Meant to run once/day via Windows Task Scheduler.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import GmailApiSender, SendingError, connect, init_db, load_settings, send_approved_emails


def main() -> None:
    settings = load_settings()
    init_db(settings.db_path, settings)

    if not settings.sender_email:
        print("sender.email is not set in config/settings.yaml. Fill it in first.")
        raise SystemExit(1)

    try:
        sender = GmailApiSender(settings.sender_email, settings.gmail_token_path)
    except SendingError as exc:
        print(str(exc))
        raise SystemExit(1)

    with connect(settings.db_path) as conn:
        stats = send_approved_emails(
            conn,
            sender,
            resume_pdf_path=settings.resume_pdf_path,
            bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
            today=date.today(),
            min_delay_seconds=settings.send.min_delay_seconds,
            max_delay_seconds=settings.send.max_delay_seconds,
        )

    if stats.circuit_breaker_tripped:
        print("Circuit breaker tripped: the most recent day's bounce rate exceeded the")
        print("threshold. Nothing was sent. Review send_log and email_queue before retrying.")
        raise SystemExit(1)

    print(f"Daily cap: {stats.daily_cap} (remaining capacity before this run: {stats.remaining_capacity})")
    print(f"Sent: {stats.sent}")
    if stats.failed:
        print(f"Failed to send: {stats.failed} (left status='approved' for retry next run)")


if __name__ == "__main__":
    main()
