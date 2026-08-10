"""Review queue UI for the cold-email job agent. See PROJECT.md §4.5.

Run with: streamlit run review_app.py

Approve / Edit (body only) / Reject only — nothing here sends anything. Approving
sets status='approved', picked up later by the scheduled sender (module 7); there's
always a buffer between "approved" and "actually left the outbox."
"""

from __future__ import annotations

import streamlit as st

from agent import connect, init_db, load_settings
from agent.review_queue import approve_draft, list_pending_drafts, reject_draft

st.set_page_config(page_title="Cold Email Review Queue", layout="wide")

settings = load_settings()
init_db(settings.db_path, settings)

st.title("Cold Email Review Queue")

with connect(settings.db_path) as conn:
    drafts = list_pending_drafts(conn)

if not drafts:
    st.info("No drafts waiting for review.")
    st.stop()

st.caption(f"{len(drafts)} draft(s) pending review")

for draft in drafts:
    with st.container(border=True):
        header_col, resume_col = st.columns([4, 1])
        with header_col:
            st.subheader(f"{draft.company_name} — {draft.contact_name or 'no contact name'}")
            st.caption(
                f"{draft.contact_email} · niche: {draft.niche or 'unclassified'} · "
                f"kind: {draft.kind} · queued: {draft.created_at}"
            )
        with resume_col:
            st.metric("Resume attached", "Yes" if draft.attached_resume else "No")

        st.text_input(
            "Subject", value=draft.subject, key=f"subject_{draft.email_queue_id}", disabled=True
        )
        edited_body = st.text_area(
            "Body", value=draft.body, height=240, key=f"body_{draft.email_queue_id}"
        )

        approve_col, reject_col, _ = st.columns([1, 1, 4])
        with approve_col:
            if st.button("Approve", key=f"approve_{draft.email_queue_id}", type="primary"):
                with connect(settings.db_path) as conn:
                    approve_draft(conn, draft.email_queue_id, edited_body=edited_body)
                st.rerun()
        with reject_col:
            if st.button("Reject", key=f"reject_{draft.email_queue_id}"):
                with connect(settings.db_path) as conn:
                    reject_draft(conn, draft.email_queue_id)
                st.rerun()
