"""Tracking dashboard data access for the Streamlit UI. See PROJECT.md §4.7/§4.5.

Kept separate from review_app.py (same split as agent/review_queue.py) so this is
pytest-testable without a Streamlit harness. Two distinct signals, both surfaced here,
mirroring what a friend's cold-email spreadsheet (reverse-engineered this session) got
right by splitting them across two sheets:
  - the *automated* per-contact stage (sent / replied / no_response / bounced / ...),
    driven entirely by agent/tracking.py + agent/followups.py acting on real Gmail data
  - the *manual* per-company outcome (did_not_reply / rejected / got_referral /
    interview / ...), which only Arjun can know by actually reading his inbox -- never
    inferred from the automated stage.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from agent.followups import business_days_since

ALLOWED_OUTCOME_STATUSES = {
    "did_not_reply",
    "rejected",
    "got_referral",
    "intern_call",
    "interview",
    "on_hold",
    "offer_received",
}

_STAGE_LABELS = {
    "pending_review": "Draft pending review",
    "rejected": "Draft rejected",
    "approved": "Approved, awaiting send",
    "replied": "Replied",
    "bounced": "Bounced",
    "no_response": "No response",
}


class TrackingError(Exception):
    """Raised when an invalid outcome_status is set."""


@dataclass(frozen=True)
class TrackingRow:
    company_id: int
    company_name: str
    contact_name: str | None
    contact_email: str
    niche: str | None
    stage: str
    followup_number: int
    last_sent: str | None
    days_since: int | None
    outcome_status: str | None
    outcome_notes: str | None


def _stage_label(status: str, followup_number: int) -> str:
    if status == "sent":
        return "Initial sent" if followup_number == 0 else f"Follow-up {followup_number} sent"
    return _STAGE_LABELS.get(status, status)


def dashboard_rows(conn: sqlite3.Connection, today: date | None = None) -> list[TrackingRow]:
    """One row per contact who has at least one email_queue row -- their most-advanced
    row (highest followup_number, tie-broken by most recent) represents current state.
    """
    today = today or date.today()
    rows = conn.execute(
        """
        SELECT eq.followup_number, eq.status, eq.sent_at, eq.niche,
               ct.name AS contact_name, ct.email AS contact_email,
               co.id AS company_id, co.name AS company_name,
               co.outcome_status, co.outcome_notes
        FROM email_queue eq
        JOIN contacts ct ON ct.id = eq.contact_id
        JOIN companies co ON co.id = eq.company_id
        WHERE eq.id = (
            SELECT eq2.id FROM email_queue eq2
            WHERE eq2.contact_id = eq.contact_id
            ORDER BY eq2.followup_number DESC, eq2.created_at DESC, eq2.id DESC
            LIMIT 1
        )
        ORDER BY co.name ASC, ct.name ASC
        """
    ).fetchall()

    result = []
    for row in rows:
        days_since = business_days_since(row["sent_at"], today) if row["sent_at"] else None
        result.append(
            TrackingRow(
                company_id=row["company_id"],
                company_name=row["company_name"],
                contact_name=row["contact_name"],
                contact_email=row["contact_email"],
                niche=row["niche"],
                stage=_stage_label(row["status"], row["followup_number"]),
                followup_number=row["followup_number"],
                last_sent=row["sent_at"],
                days_since=days_since,
                outcome_status=row["outcome_status"],
                outcome_notes=row["outcome_notes"],
            )
        )
    return result


@dataclass(frozen=True)
class TrackingSummary:
    sent: int = 0
    replied: int = 0
    no_response: int = 0
    bounced: int = 0
    awaiting_send: int = 0


def summarize(conn: sqlite3.Connection) -> TrackingSummary:
    """Counts contacts by their current stage -- each contact's *latest* email_queue
    row only (same "current state" view as dashboard_rows), not a raw GROUP BY over
    every row, which would double-count a contact who has both an old 'sent' initial
    and a newer 'approved' follow-up queued behind it.
    """
    counts = dict(
        conn.execute(
            """
            SELECT eq.status, COUNT(*) FROM email_queue eq
            WHERE eq.id = (
                SELECT eq2.id FROM email_queue eq2
                WHERE eq2.contact_id = eq.contact_id
                ORDER BY eq2.followup_number DESC, eq2.created_at DESC, eq2.id DESC
                LIMIT 1
            )
            GROUP BY eq.status
            """
        ).fetchall()
    )
    return TrackingSummary(
        sent=counts.get("sent", 0),
        replied=counts.get("replied", 0),
        no_response=counts.get("no_response", 0),
        bounced=counts.get("bounced", 0),
        awaiting_send=counts.get("approved", 0),
    )


def set_company_outcome(
    conn: sqlite3.Connection, company_id: int, status: str | None, notes: str | None
) -> None:
    if status is not None and status not in ALLOWED_OUTCOME_STATUSES:
        raise TrackingError(
            f"invalid outcome_status {status!r}; must be one of {sorted(ALLOWED_OUTCOME_STATUSES)}"
        )
    conn.execute(
        "UPDATE companies SET outcome_status = ?, outcome_notes = ? WHERE id = ?",
        (status, notes, company_id),
    )
    conn.commit()
