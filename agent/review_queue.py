"""Review queue data access for the Streamlit UI. See PROJECT.md §4.5.

Kept separate from review_app.py (the Streamlit entry point) so this logic is
pytest-testable without a Streamlit test harness. Nothing here sends anything —
approving only flips status to 'approved', picked up later by the scheduled
sender (module 7).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingDraft:
    email_queue_id: int
    company_name: str
    contact_name: str | None
    contact_email: str
    niche: str | None
    kind: str
    subject: str
    body: str
    attached_resume: bool
    created_at: str


def _row_to_draft(row: sqlite3.Row) -> PendingDraft:
    return PendingDraft(
        email_queue_id=row["email_queue_id"],
        company_name=row["company_name"],
        contact_name=row["contact_name"],
        contact_email=row["contact_email"],
        niche=row["niche"],
        kind=row["kind"],
        subject=row["subject"],
        body=row["body"],
        attached_resume=bool(row["attached_resume"]),
        created_at=row["created_at"],
    )


def list_pending_drafts(conn: sqlite3.Connection) -> list[PendingDraft]:
    rows = conn.execute(
        """
        SELECT eq.id AS email_queue_id, eq.subject, eq.body, eq.niche, eq.kind,
               eq.attached_resume, eq.created_at,
               co.name AS company_name, ct.name AS contact_name, ct.email AS contact_email
        FROM email_queue eq
        JOIN companies co ON co.id = eq.company_id
        JOIN contacts ct ON ct.id = eq.contact_id
        WHERE eq.status = 'pending_review'
        ORDER BY eq.created_at ASC, eq.id ASC
        """
    ).fetchall()
    return [_row_to_draft(row) for row in rows]


def approve_draft(
    conn: sqlite3.Connection,
    email_queue_id: int,
    edited_body: str | None = None,
    edited_subject: str | None = None,
) -> bool:
    """Marks a pending draft approved, optionally saving an edited subject/body first.

    Returns False (no-op) if the row isn't currently pending_review — e.g. it was
    already actioned in another browser tab.
    """
    sets = ["status = 'approved'"]
    params: list[str] = []
    if edited_subject is not None:
        sets.append("subject = ?")
        params.append(edited_subject)
    if edited_body is not None:
        sets.append("body = ?")
        params.append(edited_body)
    params.append(str(email_queue_id))

    cursor = conn.execute(
        f"UPDATE email_queue SET {', '.join(sets)} WHERE id = ? AND status = 'pending_review'",
        params,
    )
    conn.commit()
    return cursor.rowcount > 0


def approve_drafts_bulk(
    conn: sqlite3.Connection,
    edits: list[tuple[int, str | None, str | None]],
) -> int:
    """Approves multiple pending drafts in one action, each optionally with its own
    edited (subject, body) -- the dashboard's "Approve Selected" button. Each item is
    (email_queue_id, edited_subject, edited_body); pass None for a field to leave it
    untouched. Returns how many rows were actually approved (rows already actioned
    elsewhere are silently skipped, same no-op semantics as approve_draft).
    """
    approved = 0
    for email_queue_id, edited_subject, edited_body in edits:
        if approve_draft(
            conn, email_queue_id, edited_body=edited_body, edited_subject=edited_subject
        ):
            approved += 1
    return approved


def reject_draft(conn: sqlite3.Connection, email_queue_id: int) -> bool:
    cursor = conn.execute(
        "UPDATE email_queue SET status = 'rejected' WHERE id = ? AND status = 'pending_review'",
        (email_queue_id,),
    )
    conn.commit()
    return cursor.rowcount > 0
