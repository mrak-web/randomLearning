"""Daily reply/bounce check + follow-up generation. See PROJECT.md §4.7.

Usage: py scripts/check_replies.py

Meant to run once a day via Windows Task Scheduler, before scripts/send_batch.py --
freshly auto-approved follow-ups (see agent/followups.py) need send_batch.py's next
run to actually go out. Order matters: bounces and replies are checked before
generating follow-ups, so a contact whose bounce/reply was just detected this run
doesn't also get a follow-up queued in the same run.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import (
    GmailReplyChecker,
    SendingError,
    check_bounces,
    check_replies,
    connect,
    generate_due_followups,
    init_db,
    load_settings,
)


def main() -> None:
    settings = load_settings()
    init_db(settings.db_path, settings)

    if not settings.sender_email:
        print("sender.email is not set in config/settings.yaml. Fill it in first.")
        raise SystemExit(1)

    try:
        checker = GmailReplyChecker(settings.gmail_token_path)
    except SendingError as exc:
        print(str(exc))
        raise SystemExit(1)

    today = date.today()
    with connect(settings.db_path) as conn:
        bounced = check_bounces(conn, checker)
        replied = check_replies(conn, checker, settings.sender_email)
        followup_stats = generate_due_followups(
            conn,
            templates_dir=settings.templates_dir,
            sender_display_name=settings.sender_display_name,
            business_days_wait=settings.followup.business_days_wait,
            max_followups=settings.followup.max_followups,
            sender_phone=settings.sender_phone,
            today=today,
        )

    print(f"Bounces detected: {bounced}")
    print(f"Contacts newly marked replied: {replied}")
    print(f"Follow-ups auto-approved (will send on next send_batch.py run): {followup_stats.generated}")
    print(f"Contacts retired as no_response: {followup_stats.retired_no_response}")


if __name__ == "__main__":
    main()
