"""Follow-up generation for the cold-email job agent. See PROJECT.md §4.7.

Follow-ups auto-send once due (Arjun's explicit choice, 2026-08-29) -- unlike initial
emails, rows here are inserted with status='approved' rather than 'pending_review',
skipping the review queue entirely and feeding straight into the existing
send_approved_emails path (agent/sending.py) on the next send_batch.py run. Templates
are not niche-specific (config/templates/followup{1,2}.txt) since RESUME_STORY_NICHE
already means every initial email uses the same Rapido content regardless of niche.

Follow-ups don't re-attach the resume -- it's already visible earlier in the same
Gmail thread (see agent/gmail_client.py's threading), so re-attaching would just be
redundant weight on a short bump email.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from agent.email_generation import render_followup_email


def business_days_since(sent_at: str, today: date) -> int:
    """Counts weekdays strictly after sent_at's date, up to and including today.

    No holiday calendar -- the same "not perfect precision by design" tradeoff already
    accepted elsewhere in this project (see PROJECT.md's classifier/relevance-filter
    notes) rather than a hard requirement here.
    """
    sent_date = datetime.fromisoformat(sent_at).date()
    if today <= sent_date:
        return 0
    count = 0
    d = sent_date + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


@dataclass(frozen=True)
class FollowupStats:
    generated: int = 0
    retired_no_response: int = 0


def _latest_email_queue_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per contact: their most-advanced email_queue row (highest
    followup_number, tie-broken by most recent) -- i.e. the contact's current state.
    A freshly auto-approved-but-not-yet-sent follow-up row is itself the latest row
    for its contact (status='approved'), which is what keeps this function from
    generating a duplicate follow-up before send_batch.py has picked the last one up.
    """
    return conn.execute(
        """
        SELECT eq.id AS email_queue_id, eq.contact_id, eq.company_id, eq.niche,
               eq.followup_number, eq.status, eq.sent_at, eq.gmail_thread_id,
               ct.name AS contact_name, co.name AS company_name
        FROM email_queue eq
        JOIN contacts ct ON ct.id = eq.contact_id
        JOIN companies co ON co.id = eq.company_id
        WHERE eq.id = (
            SELECT eq2.id FROM email_queue eq2
            WHERE eq2.contact_id = eq.contact_id
            ORDER BY eq2.followup_number DESC, eq2.created_at DESC, eq2.id DESC
            LIMIT 1
        )
        """
    ).fetchall()


def generate_due_followups(
    conn: sqlite3.Connection,
    *,
    templates_dir: Path,
    sender_display_name: str,
    business_days_wait: int,
    max_followups: int,
    sender_phone: str = "",
    today: date | None = None,
) -> FollowupStats:
    """For every contact whose latest sent email is due for its next follow-up round,
    drafts and auto-approves the next round. Contacts already at max_followups whose
    wait period has elapsed with no reply get retired to status='no_response' instead.
    """
    today = today or date.today()
    generated = 0
    retired = 0

    for row in _latest_email_queue_rows(conn):
        if row["status"] != "sent" or row["sent_at"] is None:
            continue  # replied / bounced / still pending review or approval

        elapsed = business_days_since(row["sent_at"], today)
        if elapsed < business_days_wait:
            continue

        next_round = row["followup_number"] + 1
        if next_round > max_followups:
            conn.execute(
                "UPDATE email_queue SET status = 'no_response' WHERE id = ?",
                (row["email_queue_id"],),
            )
            retired += 1
            continue

        template_path = templates_dir / f"followup{next_round}.txt"
        template_text = template_path.read_text(encoding="utf-8")
        subject, body = render_followup_email(
            template_text,
            company_name=row["company_name"],
            contact_name=row["contact_name"],
            sender_phone=sender_phone,
            sender_display_name=sender_display_name,
        )

        conn.execute(
            """
            INSERT INTO email_queue
                (contact_id, company_id, niche, subject, body, kind, followup_number,
                 attached_resume, status, gmail_thread_id)
            VALUES (?, ?, ?, ?, ?, 'followup', ?, 0, 'approved', ?)
            """,
            (
                row["contact_id"],
                row["company_id"],
                row["niche"],
                subject,
                body,
                next_round,
                row["gmail_thread_id"],
            ),
        )
        generated += 1

    conn.commit()
    return FollowupStats(generated=generated, retired_no_response=retired)
