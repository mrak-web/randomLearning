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

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openpyxl

RAW_SHEET = "raw"
CANDIDATES_SHEET = "Pipeline Candidates"
CANDIDATES_HEADER = [
    "Company", "Contact Name", "Designation", "Email", "Domain", "Status", "First Seen", "Note",
]


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
