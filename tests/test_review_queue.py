from __future__ import annotations

import dataclasses
import itertools
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db
from agent.review_queue import approve_draft, approve_drafts_bulk, list_pending_drafts, reject_draft

_email_counter = itertools.count()


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


def _seed_draft(
    conn,
    *,
    company_name="Meesho",
    contact_name="Priya Sharma",
    contact_email=None,
    niche="consumer",
    subject="Product role at Meesho",
    body="Hi Priya, ...",
    status="pending_review",
    created_at=None,
) -> int:
    if contact_email is None:
        contact_email = f"contact{next(_email_counter)}@example.com"

    company_id = conn.execute(
        "INSERT INTO companies (name, source, niche, status) VALUES (?, 'manual_csv', ?, 'classified')",
        (company_name, niche),
    ).lastrowid
    contact_id = conn.execute(
        "INSERT INTO contacts (company_id, name, email, role_category, status) "
        "VALUES (?, ?, ?, 'hr', 'verified')",
        (company_id, contact_name, contact_email),
    ).lastrowid
    if created_at is not None:
        email_queue_id = conn.execute(
            "INSERT INTO email_queue "
            "(contact_id, company_id, niche, subject, body, kind, attached_resume, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'initial', 1, ?, ?)",
            (contact_id, company_id, niche, subject, body, status, created_at),
        ).lastrowid
    else:
        email_queue_id = conn.execute(
            "INSERT INTO email_queue "
            "(contact_id, company_id, niche, subject, body, kind, attached_resume, status) "
            "VALUES (?, ?, ?, ?, ?, 'initial', 1, ?)",
            (contact_id, company_id, niche, subject, body, status),
        ).lastrowid
    conn.commit()
    return email_queue_id


def test_list_pending_drafts_returns_joined_fields(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _seed_draft(conn, contact_email="priya@meesho.example")

        drafts = list_pending_drafts(conn)

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.company_name == "Meesho"
    assert draft.contact_name == "Priya Sharma"
    assert draft.contact_email == "priya@meesho.example"
    assert draft.niche == "consumer"
    assert draft.kind == "initial"
    assert draft.attached_resume is True
    assert draft.subject == "Product role at Meesho"


def test_list_pending_drafts_excludes_non_pending_status(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _seed_draft(conn, status="approved")
        _seed_draft(conn, status="rejected")
        _seed_draft(conn, status="sent")

        drafts = list_pending_drafts(conn)

    assert drafts == []


def test_list_pending_drafts_orders_oldest_first(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        _seed_draft(conn, company_name="Newer Co", created_at="2026-01-02 00:00:00")
        _seed_draft(conn, company_name="Older Co", created_at="2026-01-01 00:00:00")

        drafts = list_pending_drafts(conn)

    assert [d.company_name for d in drafts] == ["Older Co", "Newer Co"]


def test_approve_draft_sets_status_and_keeps_body_when_not_edited(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        email_queue_id = _seed_draft(conn, body="Original body")

        result = approve_draft(conn, email_queue_id)

        row = conn.execute(
            "SELECT status, body FROM email_queue WHERE id = ?", (email_queue_id,)
        ).fetchone()

    assert result is True
    assert row["status"] == "approved"
    assert row["body"] == "Original body"


def test_approve_draft_saves_edited_body(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        email_queue_id = _seed_draft(conn, body="Original body")

        approve_draft(conn, email_queue_id, edited_body="Edited body")

        row = conn.execute(
            "SELECT status, body FROM email_queue WHERE id = ?", (email_queue_id,)
        ).fetchone()

    assert row["status"] == "approved"
    assert row["body"] == "Edited body"


def test_approve_draft_saves_edited_subject(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        email_queue_id = _seed_draft(conn, subject="Original subject")

        approve_draft(conn, email_queue_id, edited_subject="Edited subject")

        row = conn.execute(
            "SELECT status, subject FROM email_queue WHERE id = ?", (email_queue_id,)
        ).fetchone()

    assert row["status"] == "approved"
    assert row["subject"] == "Edited subject"


def test_approve_draft_saves_edited_subject_and_body_together(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        email_queue_id = _seed_draft(conn, subject="Original subject", body="Original body")

        approve_draft(
            conn, email_queue_id, edited_subject="New subject", edited_body="New body"
        )

        row = conn.execute(
            "SELECT subject, body FROM email_queue WHERE id = ?", (email_queue_id,)
        ).fetchone()

    assert row["subject"] == "New subject"
    assert row["body"] == "New body"


def test_approve_draft_is_a_noop_on_already_actioned_row(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        email_queue_id = _seed_draft(conn, status="rejected")

        result = approve_draft(conn, email_queue_id)

        row = conn.execute("SELECT status FROM email_queue WHERE id = ?", (email_queue_id,)).fetchone()

    assert result is False
    assert row["status"] == "rejected"


def test_reject_draft_sets_status(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        email_queue_id = _seed_draft(conn)

        result = reject_draft(conn, email_queue_id)

        row = conn.execute("SELECT status FROM email_queue WHERE id = ?", (email_queue_id,)).fetchone()

    assert result is True
    assert row["status"] == "rejected"


def test_reject_draft_is_a_noop_on_already_actioned_row(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        email_queue_id = _seed_draft(conn, status="approved")

        result = reject_draft(conn, email_queue_id)

        row = conn.execute("SELECT status FROM email_queue WHERE id = ?", (email_queue_id,)).fetchone()

    assert result is False
    assert row["status"] == "approved"


def test_approve_drafts_bulk_approves_all_with_their_own_edits(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        id_a = _seed_draft(conn, company_name="Co A", subject="Subject A", body="Body A")
        id_b = _seed_draft(conn, company_name="Co B", subject="Subject B", body="Body B")

        approved_count = approve_drafts_bulk(
            conn,
            [
                (id_a, "Edited A", None),
                (id_b, None, "Edited body B"),
            ],
        )

        row_a = conn.execute("SELECT status, subject, body FROM email_queue WHERE id = ?", (id_a,)).fetchone()
        row_b = conn.execute("SELECT status, subject, body FROM email_queue WHERE id = ?", (id_b,)).fetchone()

    assert approved_count == 2
    assert row_a["status"] == "approved"
    assert row_a["subject"] == "Edited A"
    assert row_a["body"] == "Body A"  # untouched -- None means "leave as-is"
    assert row_b["status"] == "approved"
    assert row_b["subject"] == "Subject B"  # untouched
    assert row_b["body"] == "Edited body B"


def test_approve_drafts_bulk_skips_already_actioned_rows(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        id_pending = _seed_draft(conn, company_name="Still Pending")
        id_rejected = _seed_draft(conn, company_name="Already Rejected", status="rejected")

        approved_count = approve_drafts_bulk(conn, [(id_pending, None, None), (id_rejected, None, None)])

        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM email_queue").fetchall()
        }

    assert approved_count == 1
    assert statuses[id_pending] == "approved"
    assert statuses[id_rejected] == "rejected"  # left alone, not overwritten


def test_approved_and_rejected_drafts_disappear_from_queue(settings):
    init_db(settings.db_path, settings)
    with connect(settings.db_path) as conn:
        keep_id = _seed_draft(conn, company_name="Stays Pending")
        approve_id = _seed_draft(conn, company_name="Gets Approved")
        reject_id = _seed_draft(conn, company_name="Gets Rejected")

        approve_draft(conn, approve_id)
        reject_draft(conn, reject_id)

        drafts = list_pending_drafts(conn)

    assert [d.email_queue_id for d in drafts] == [keep_id]
