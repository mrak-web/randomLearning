"""Review queue + tracking dashboard for the cold-email job agent. See PROJECT.md
§4.5/§4.7.

Run with: streamlit run review_app.py

Tab 1 (Review Queue): Approve / Edit (body only) / Reject for initial-email drafts --
nothing here sends anything. Approving sets status='approved', picked up later by the
scheduled sender (module 7). Follow-ups never appear here -- they're auto-approved by
agent/followups.py once due (Arjun's explicit choice, 2026-08-29), so this tab is
initial emails only.

Tab 2 (Tracking Dashboard): read-only automated stage per contact (sent / replied /
no_response / bounced / ...), plus a manual per-company outcome editor (did_not_reply /
rejected / got_referral / interview / ...) -- the real funnel signal, which only Arjun
can know by reading his inbox. Mirrors the two-sheet split found in a friend's
cold-email tracker this session reverse-engineered: automated status and manually
curated outcome are deliberately kept separate, never inferred from each other.
"""

from __future__ import annotations

import streamlit as st

from agent import (
    ALLOWED_OUTCOME_STATUSES,
    connect,
    dashboard_rows,
    init_db,
    load_settings,
    set_company_outcome,
    summarize,
)
from agent.review_queue import approve_draft, list_pending_drafts, reject_draft

st.set_page_config(page_title="Cold Email Agent", layout="wide")

settings = load_settings()
init_db(settings.db_path, settings)

review_tab, dashboard_tab = st.tabs(["Review Queue", "Tracking Dashboard"])

with review_tab:
    st.title("Cold Email Review Queue")

    with connect(settings.db_path) as conn:
        drafts = list_pending_drafts(conn)

    if not drafts:
        st.info("No drafts waiting for review.")
    else:
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

with dashboard_tab:
    st.title("Tracking Dashboard")

    with connect(settings.db_path) as conn:
        summary = summarize(conn)
        rows = dashboard_rows(conn)

    metric_cols = st.columns(5)
    metric_cols[0].metric("Sent, awaiting reply", summary.sent)
    metric_cols[1].metric("Replied", summary.replied)
    metric_cols[2].metric("No response", summary.no_response)
    metric_cols[3].metric("Bounced", summary.bounced)
    metric_cols[4].metric("Follow-ups awaiting send", summary.awaiting_send)

    st.subheader("Contacts")
    if not rows:
        st.info("No tracked contacts yet — generate and send some emails first.")
    else:
        st.dataframe(
            [
                {
                    "Company": r.company_name,
                    "Contact": r.contact_name or "—",
                    "Email": r.contact_email,
                    "Niche": r.niche or "unclassified",
                    "Stage": r.stage,
                    "Last Sent": r.last_sent or "—",
                    "Business Days Since": r.days_since if r.days_since is not None else "—",
                }
                for r in rows
            ],
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Company Outcomes")
    st.caption(
        "Manual — only you know the real outcome from your inbox. Automated stage above "
        "is a hint, not a substitute (e.g. 'no response' just means no automated reply "
        "was detected, not that the company definitely passed)."
    )

    companies = {}
    for r in rows:
        companies.setdefault(
            r.company_id, {"name": r.company_name, "status": r.outcome_status, "notes": r.outcome_notes}
        )

    status_options = ["(not set)"] + sorted(ALLOWED_OUTCOME_STATUSES)
    for company_id, info in sorted(companies.items(), key=lambda kv: kv[1]["name"]):
        with st.container(border=True):
            cols = st.columns([2, 2, 3, 1])
            cols[0].markdown(f"**{info['name']}**")
            current_index = (
                status_options.index(info["status"]) if info["status"] in status_options else 0
            )
            new_status = cols[1].selectbox(
                "Outcome",
                status_options,
                index=current_index,
                key=f"outcome_status_{company_id}",
                label_visibility="collapsed",
            )
            new_notes = cols[2].text_input(
                "Notes",
                value=info["notes"] or "",
                key=f"outcome_notes_{company_id}",
                label_visibility="collapsed",
                placeholder="Notes (optional)",
            )
            if cols[3].button("Save", key=f"save_outcome_{company_id}"):
                with connect(settings.db_path) as conn:
                    set_company_outcome(
                        conn,
                        company_id,
                        None if new_status == "(not set)" else new_status,
                        new_notes or None,
                    )
                st.rerun()
