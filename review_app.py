"""Review queue + tracking dashboard for the cold-email job agent. See PROJECT.md
§4.5/§4.7.

Run with: streamlit run review_app.py

Tab 1 (Review Queue): Approve / Edit (subject + body) / Reject for initial-email
drafts, one at a time or in bulk via checkboxes -- nothing here sends anything.
Approving sets status='approved', picked up by the scheduled sender (module 7).
Follow-ups never appear here -- they're auto-approved by agent/followups.py once due
(Arjun's explicit choice, 2026-08-29), so this tab is initial emails only.

Tab 2 (Tracking Dashboard): read-only automated stage per contact (sent / replied /
no_response / bounced / ...), the manual per-company outcome editor, and two on-demand
action buttons. The *actual* daily send happens via a Windows Task Scheduler entry at a
fixed time (10:30am, see PROJECT.md §4.6) -- decoupling when Arjun reviews/approves
from when email leaves the outbox, since nothing sends until that scheduled trigger
runs regardless of when he approves. "Send Now" here is a manual override for testing
or an urgent same-day send, not the normal path.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from agent import (
    ALLOWED_OUTCOME_STATUSES,
    MASTERSHEET_PATH,
    SCOPES,
    GmailApiSender,
    GmailReplyChecker,
    SendingError,
    check_bounces,
    check_gmail_token,
    check_replies,
    check_scheduler_status,
    connect,
    count_new_candidates,
    dashboard_rows,
    generate_due_followups,
    import_next_batch,
    init_db,
    is_weekend,
    load_niche_keywords,
    load_settings,
    load_story_bank,
    send_approved_emails,
    set_company_outcome,
    summarize,
)
from agent.review_queue import approve_draft, approve_drafts_bulk, list_pending_drafts, reject_draft

st.set_page_config(page_title="Cold Email Agent", layout="wide")

settings = load_settings()
init_db(settings.db_path, settings)

review_tab, dashboard_tab = st.tabs(["Review Queue", "Tracking Dashboard"])

with review_tab:
    st.title("Cold Email Review Queue")

    st.subheader("Pipeline Candidates")
    remaining_candidates = count_new_candidates(MASTERSHEET_PATH)
    pull_col, count_col = st.columns([1, 3])
    with pull_col:
        pull_size = min(10, remaining_candidates)
        if st.button(f"Pull {pull_size} more" if pull_size else "Pull more", disabled=pull_size == 0):
            story_bank = load_story_bank(settings.story_bank_path)
            niche_keywords = load_niche_keywords(settings.niche_keywords_path)
            with connect(settings.db_path) as conn:
                batch_stats = import_next_batch(
                    conn,
                    MASTERSHEET_PATH,
                    story_bank=story_bank,
                    templates_dir=settings.templates_dir,
                    sender_display_name=settings.sender_display_name,
                    attach_resume_by_default=settings.attach_resume_by_default,
                    sender_phone=settings.sender_phone,
                    niche_keywords=niche_keywords,
                    niche_order=settings.niches.order,
                    batch_size=10,
                )
            st.success(
                f"Pulled {batch_stats.candidates_pulled} candidate(s): "
                f"{batch_stats.drafts_generated} draft(s) generated, "
                f"{batch_stats.companies_unclassifiable} company(s) couldn't be classified yet "
                f"(no niche match, no draft until fixed). {batch_stats.remaining_new} left in the tracker."
            )
            st.rerun()
    with count_col:
        st.caption(
            f"{remaining_candidates} candidate(s) waiting in the mastersheet tracker "
            "(data/Email Mastersheet.xlsx, 'Pipeline Candidates' sheet)."
        )

    with connect(settings.db_path) as conn:
        drafts = list_pending_drafts(conn)

    if not drafts:
        st.info("No drafts waiting for review.")
    else:
        st.caption(f"{len(drafts)} draft(s) pending review")

        def _apply_select_all():
            # Runs before the rerun that draws the per-row checkboxes below, so it
            # can set their session_state directly -- a checkbox's `value=` argument
            # is only honored the very first time its key is created, not on later
            # reruns, so toggling "Select all" wouldn't otherwise touch checkboxes
            # that already exist.
            value = st.session_state["select_all_drafts"]
            for draft in drafts:
                st.session_state[f"select_{draft.email_queue_id}"] = value

        select_all_col, approve_selected_col, _ = st.columns([1, 2, 4])
        with select_all_col:
            st.checkbox("Select all", key="select_all_drafts", on_change=_apply_select_all)

        for draft in drafts:
            with st.container(border=True):
                select_col, header_col, resume_col = st.columns([0.3, 4, 1])
                with select_col:
                    st.session_state.setdefault(f"select_{draft.email_queue_id}", False)
                    st.checkbox(
                        "Select",
                        key=f"select_{draft.email_queue_id}",
                        label_visibility="collapsed",
                    )
                with header_col:
                    st.subheader(f"{draft.company_name} — {draft.contact_name or 'no contact name'}")
                    st.caption(
                        f"{draft.contact_email} · niche: {draft.niche or 'unclassified'} · "
                        f"kind: {draft.kind} · queued: {draft.created_at}"
                    )
                with resume_col:
                    st.metric("Resume attached", "Yes" if draft.attached_resume else "No")

                edited_subject = st.text_input(
                    "Subject", value=draft.subject, key=f"subject_{draft.email_queue_id}"
                )
                edited_body = st.text_area(
                    "Body", value=draft.body, height=240, key=f"body_{draft.email_queue_id}"
                )

                approve_col, reject_col, _ = st.columns([1, 1, 4])
                with approve_col:
                    if st.button("Approve", key=f"approve_{draft.email_queue_id}", type="primary"):
                        with connect(settings.db_path) as conn:
                            approve_draft(
                                conn,
                                draft.email_queue_id,
                                edited_body=edited_body,
                                edited_subject=edited_subject,
                            )
                        st.rerun()
                with reject_col:
                    if st.button("Reject", key=f"reject_{draft.email_queue_id}"):
                        with connect(settings.db_path) as conn:
                            reject_draft(conn, draft.email_queue_id)
                        st.rerun()

        selected_ids = [
            draft.email_queue_id
            for draft in drafts
            if st.session_state.get(f"select_{draft.email_queue_id}")
        ]
        with approve_selected_col:
            if st.button(
                f"Approve Selected ({len(selected_ids)})",
                type="primary",
                disabled=not selected_ids,
            ):
                edits = [
                    (
                        eq_id,
                        st.session_state.get(f"subject_{eq_id}"),
                        st.session_state.get(f"body_{eq_id}"),
                    )
                    for eq_id in selected_ids
                ]
                with connect(settings.db_path) as conn:
                    approved_count = approve_drafts_bulk(conn, edits)
                st.success(f"Approved {approved_count} draft(s).")
                st.rerun()

with dashboard_tab:
    st.title("Tracking Dashboard")

    st.subheader("System Health")
    st.caption(
        "Live checks for the two things that can silently stop the daily send: the "
        "Gmail token and the Task Scheduler entry. Checked fresh on every page load."
    )
    health_col1, health_col2 = st.columns(2)

    with health_col1:
        token_status = check_gmail_token(settings.gmail_token_path, SCOPES)
        if token_status.ok:
            st.success(f"Gmail token: {token_status.detail}")
        else:
            st.error(f"Gmail token: {token_status.detail}")

    with health_col2:
        scheduler_status = check_scheduler_status("ColdEmailAgent_DailyRun")
        if scheduler_status.ok:
            st.success(f"Scheduled task: {scheduler_status.detail}")
        else:
            st.error(f"Scheduled task: {scheduler_status.detail}")
        if scheduler_status.next_run_time or scheduler_status.last_run_time:
            st.caption(
                f"Next run: {scheduler_status.next_run_time or '—'} · "
                f"Last run: {scheduler_status.last_run_time or '—'} · "
                f"Logon mode: {scheduler_status.logon_mode or '—'}"
            )

    st.subheader("Actions")
    st.caption(
        "The daily 10:30am Task Scheduler run already does both of these automatically. "
        "Use these only to check something right now or to override the schedule."
    )
    action_col1, action_col2 = st.columns(2)

    with action_col1:
        if st.button("Check Replies & Follow-ups Now"):
            try:
                checker = GmailReplyChecker(settings.gmail_token_path)
            except SendingError as exc:
                st.error(str(exc))
            else:
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
                        today=date.today(),
                    )
                st.success(
                    f"Bounces detected: {bounced} · Newly replied: {replied} · "
                    f"Follow-ups auto-approved: {followup_stats.generated} · "
                    f"Retired (no response): {followup_stats.retired_no_response}"
                )
                st.rerun()

    with action_col2:
        st.caption(
            "Sends whatever is currently 'approved' right now, with the same spacing/"
            "cap/circuit-breaker safeguards as the scheduled run — with several "
            "approved emails this can take a few minutes; keep this tab open."
        )

        def _send_now(*, allow_weekend: bool) -> None:
            try:
                sender = GmailApiSender(settings.sender_email, settings.gmail_token_path)
            except SendingError as exc:
                st.error(str(exc))
                return
            with connect(settings.db_path) as conn:
                stats = send_approved_emails(
                    conn,
                    sender,
                    resume_pdf_path=settings.resume_pdf_path,
                    bounce_rate_circuit_breaker=settings.send.bounce_rate_circuit_breaker,
                    today=date.today(),
                    min_delay_seconds=settings.send.min_delay_seconds,
                    max_delay_seconds=settings.send.max_delay_seconds,
                    allow_weekend=allow_weekend,
                )
            if stats.circuit_breaker_tripped:
                st.error(
                    "Circuit breaker tripped on recent bounce rate — nothing sent. "
                    "Check send_log / email_queue before retrying."
                )
            else:
                st.success(f"Sent {stats.sent} email(s). Daily cap: {stats.daily_cap}.")
                if stats.failed:
                    st.warning(f"{stats.failed} failed to send (left approved for retry).")

        # No emails should leave the outbox on a Saturday/Sunday (Arjun's guardrail,
        # 2026-09-06) -- this button can still override it, but only after an explicit
        # confirmation click, never on the first press.
        if st.session_state.get("weekend_send_pending"):
            st.warning(
                "Today is a weekend — this pipeline doesn't send on weekends. "
                "Send anyway?"
            )
            confirm_col, cancel_col = st.columns(2)
            if confirm_col.button("Yes, send anyway", key="confirm_weekend_send"):
                st.session_state["weekend_send_pending"] = False
                _send_now(allow_weekend=True)
                st.rerun()
            if cancel_col.button("Cancel", key="cancel_weekend_send"):
                st.session_state["weekend_send_pending"] = False
                st.rerun()
        elif st.button("Send Now (bypasses the 10:30am schedule)"):
            if is_weekend(date.today()):
                st.session_state["weekend_send_pending"] = True
                st.rerun()
            else:
                _send_now(allow_weekend=False)
                st.rerun()

    with connect(settings.db_path) as conn:
        summary = summarize(conn)
        rows = dashboard_rows(conn)

    st.subheader("Summary")
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
