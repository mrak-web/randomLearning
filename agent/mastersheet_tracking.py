"""Tracks candidates from Arjun's pre-existing hand-curated cold-email mastersheet
(data/Email Mastersheet.xlsx) in a dedicated sheet inside that same workbook --
deliberately kept separate from agent.db / the Streamlit dashboard (Arjun's explicit
choice, 2026-09-09), not a new source feeding the automated pipeline.

The mastersheet's other sheets (Company, Outreach, useless) are a friend's cold-email
tracker template Arjun copied the schema from (see db/schema.sql's companies.outcome_
status comment) -- not Arjun's own outreach history, since he hasn't sent anything yet
(first batch goes out via this pipeline separately). They are never read or written
here; the only dedup source is agent.db itself, so a candidate already sourced through
the real pipeline doesn't get listed twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openpyxl

from agent.classification import classify_pending_companies
from agent.contact_discovery import categorize_role
from agent.email_generation import generate_pending_emails

RAW_SHEET = "raw"
CANDIDATES_SHEET = "Pipeline Candidates"
CANDIDATES_HEADER = [
    "Company", "Contact Name", "Designation", "Email", "Domain", "Status", "First Seen", "Note",
]
# 1-based column indices into CANDIDATES_HEADER, for reading/updating specific cells.
_COL_EMAIL, _COL_STATUS = 4, 6

MASTERSHEET_PATH = Path("data/Email Mastersheet.xlsx")
MASTERSHEET_SOURCE = "mastersheet"


def _domain_from_email(email: str) -> str | None:
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return domain or None


@dataclass(frozen=True)
class CandidateRow:
    company: str
    contact_name: str | None
    designation: str | None
    email: str
    domain: str | None
    note: str | None


def read_raw_candidates(wb: openpyxl.Workbook) -> list[CandidateRow]:
    """Rows from the 'raw' sheet with at least a company name and one email --
    everything else in that sheet (blank rows, company-only rows with no contact
    email) can't be turned into an outreach candidate, so is silently skipped rather
    than erroring, since 'raw' is a loosely-kept scratch sheet, not a strict schema.
    """
    ws = wb[RAW_SHEET]
    candidates: list[CandidateRow] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row:
            continue
        company = (row[0] or "").strip() if len(row) > 0 else ""
        if not company:
            continue
        name = (row[1] or "").strip() if len(row) > 1 and row[1] else None
        designation = (row[2] or "").strip() if len(row) > 2 and row[2] else None
        email1 = (row[3] or "").strip() if len(row) > 3 and row[3] else ""
        email2 = (row[4] or "").strip() if len(row) > 4 and row[4] else ""
        note = (row[5] or "").strip() if len(row) > 5 and row[5] else None
        email = (email1 or email2).lower()
        if not email:
            continue
        candidates.append(
            CandidateRow(
                company=company,
                contact_name=name,
                designation=designation,
                email=email,
                domain=_domain_from_email(email),
                note=note,
            )
        )
    return candidates


@dataclass(frozen=True)
class TrackingStats:
    candidates_found: int = 0
    already_in_pipeline_db: int = 0
    already_tracked: int = 0
    newly_tracked: int = 0


def _existing_tracked_emails(ws) -> set[str]:
    return {
        row[3].strip().lower()
        for row in ws.iter_rows(min_row=2, values_only=True)
        if row and len(row) > 3 and row[3]
    }


def sync_pipeline_candidates(
    xlsx_path: Path,
    existing_domains: set[str],
    existing_names: set[str],
    today: date | None = None,
) -> TrackingStats:
    """Appends newly-seen 'raw' sheet candidates to the CANDIDATES_SHEET tab (created
    if missing), skipping anything already tracked there or already present in
    agent.db (by domain, falling back to exact company name for the few agent.db rows
    with no domain). Never touches any other sheet -- existing formulas/data in the
    rest of the workbook are preserved as-is by only appending rows to one tab.
    """
    wb = openpyxl.load_workbook(xlsx_path)
    candidates = read_raw_candidates(wb)

    if CANDIDATES_SHEET in wb.sheetnames:
        ws = wb[CANDIDATES_SHEET]
        already_tracked_emails = _existing_tracked_emails(ws)
    else:
        ws = wb.create_sheet(CANDIDATES_SHEET)
        ws.append(CANDIDATES_HEADER)
        already_tracked_emails = set()

    existing_domains_lower = {d.strip().lower() for d in existing_domains if d}
    existing_names_lower = {n.strip().lower() for n in existing_names if n}

    already_in_db = 0
    already_tracked = 0
    newly_tracked = 0
    today_str = (today or date.today()).isoformat()

    for candidate in candidates:
        if candidate.email in already_tracked_emails:
            already_tracked += 1
            continue

        in_db = (candidate.domain is not None and candidate.domain in existing_domains_lower) or (
            candidate.company.strip().lower() in existing_names_lower
        )
        if in_db:
            already_in_db += 1
            continue

        ws.append(
            [
                candidate.company,
                candidate.contact_name,
                candidate.designation,
                candidate.email,
                candidate.domain,
                "New",
                today_str,
                candidate.note,
            ]
        )
        already_tracked_emails.add(candidate.email)
        newly_tracked += 1

    wb.save(xlsx_path)

    return TrackingStats(
        candidates_found=len(candidates),
        already_in_pipeline_db=already_in_db,
        already_tracked=already_tracked,
        newly_tracked=newly_tracked,
    )


def count_new_candidates(xlsx_path: Path) -> int:
    """How many Pipeline Candidates rows are still status='New' (not yet pulled into
    agent.db) -- shown on the dashboard so Arjun knows how much is left before he has
    to run sync_pipeline_candidates again for a fresh batch from 'raw'.
    """
    if not xlsx_path.exists():
        return 0
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    if CANDIDATES_SHEET not in wb.sheetnames:
        return 0
    ws = wb[CANDIDATES_SHEET]
    return sum(
        1
        for row in ws.iter_rows(min_row=2, values_only=True)
        if row and len(row) > _COL_STATUS - 1 and row[_COL_STATUS - 1] == "New"
    )


def _find_or_create_company(
    conn: sqlite3.Connection, name: str, domain: str | None, raw_tags: list[str]
) -> tuple[int, bool]:
    """Same dedupe-by-domain shape as agent/apollo_import.py's helper -- kept as its
    own small copy rather than a shared import since the two sources (Apollo CSV vs.
    this workbook) have nothing else in common and a shared abstraction would only
    couple them for no benefit.
    """
    if domain:
        existing = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
        if existing is not None:
            return existing["id"], False
    cursor = conn.execute(
        "INSERT INTO companies (name, domain, source, raw_tags) VALUES (?, ?, ?, ?)",
        (name, domain, MASTERSHEET_SOURCE, json.dumps(raw_tags)),
    )
    return cursor.lastrowid, True


@dataclass(frozen=True)
class BatchImportStats:
    candidates_pulled: int = 0
    companies_created: int = 0
    companies_matched: int = 0
    contacts_inserted: int = 0
    contacts_skipped_duplicate: int = 0
    companies_classified: int = 0
    companies_unclassifiable: int = 0
    drafts_generated: int = 0
    remaining_new: int = 0


def import_next_batch(
    conn: sqlite3.Connection,
    xlsx_path: Path,
    *,
    story_bank: dict,
    templates_dir: Path,
    sender_display_name: str,
    attach_resume_by_default: bool,
    sender_phone: str,
    niche_keywords: dict[str, list[str]],
    niche_order: list[str],
    batch_size: int = 10,
) -> BatchImportStats:
    """Pulls the next `batch_size` status='New' rows (in the order they were first
    tracked) from the Pipeline Candidates sheet into agent.db: creates/matches the
    company, inserts the contact (status='verified' -- these are Arjun's own
    hand-curated finds, trusted the same way a manually-sourced contact already is),
    classifies newly-created companies by niche, and generates drafts for whatever
    ends up verified+classified. Marks the pulled rows 'Imported' in the sheet so a
    later call continues from the next unpulled batch rather than repeating this one --
    this, plus dedupe-by-domain against agent.db, is what stops the same contact ever
    being pulled (and later emailed) twice across repeated "pull more" clicks.
    """
    wb = openpyxl.load_workbook(xlsx_path)
    if CANDIDATES_SHEET not in wb.sheetnames:
        return BatchImportStats()
    ws = wb[CANDIDATES_SHEET]

    pending_rows = [
        (idx, row)
        for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2)
        if row and len(row) > _COL_STATUS - 1 and row[_COL_STATUS - 1] == "New"
    ]
    batch = pending_rows[:batch_size]
    remaining_new = len(pending_rows) - len(batch)

    companies_created = 0
    companies_matched = 0
    contacts_inserted = 0
    contacts_skipped_duplicate = 0

    for row_idx, row in batch:
        company, contact_name, designation, email, domain, _status, _first_seen, note = row[:8]
        raw_tags = [note] if note else []

        company_id, created = _find_or_create_company(conn, company, domain, raw_tags)
        if created:
            companies_created += 1
        else:
            companies_matched += 1

        role_category = categorize_role(designation)
        try:
            conn.execute(
                """
                INSERT INTO contacts
                    (company_id, name, role, role_category, email,
                     verification_confidence, source_api, status)
                VALUES (?, ?, ?, ?, ?, 1.0, ?, 'verified')
                """,
                (company_id, contact_name, designation, role_category, email, MASTERSHEET_SOURCE),
            )
            contacts_inserted += 1
        except sqlite3.IntegrityError:
            contacts_skipped_duplicate += 1

        ws.cell(row=row_idx, column=_COL_STATUS, value="Imported")

    conn.commit()
    wb.save(xlsx_path)

    classification_stats = classify_pending_companies(conn, niche_keywords, niche_order)
    generation_stats = generate_pending_emails(
        conn,
        story_bank,
        templates_dir,
        sender_display_name,
        attach_resume_by_default,
        sender_phone=sender_phone,
    )

    return BatchImportStats(
        candidates_pulled=len(batch),
        companies_created=companies_created,
        companies_matched=companies_matched,
        contacts_inserted=contacts_inserted,
        contacts_skipped_duplicate=contacts_skipped_duplicate,
        companies_classified=classification_stats.classified,
        companies_unclassifiable=classification_stats.unclassifiable,
        drafts_generated=generation_stats.generated,
        remaining_new=remaining_new,
    )
