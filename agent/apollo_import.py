"""Apollo.io contact CSV import for the cold-email job agent. See PROJECT.md §4.1/§4.3.

Apollo's People Export gives a company AND an already-resolved contact/email in the
same row — a different shape from the rest of sourcing:
  - agent/sourcing.py's ManualCsvSource produces companies only (no contact yet).
  - agent/contact_discovery.py resolves a contact via a live Hunter.io lookup against
    an already-classified company, gated by niches.active_for_discovery to conserve
    free-tier quota.
This importer is a third, parallel path: for each row, find-or-create the company,
then insert the contact directly — no Hunter.io lookup spent, and not gated by
niches.active_for_discovery (Arjun hand-curated this list in Apollo himself, already
scoped to companies/contacts he wants, regardless of niche). The company still needs
scripts/classify_companies.py run (or re-run) afterward before
scripts/generate_emails.py will pick up its contacts — classification is a separate,
unaffected step, same as for any other source.

Only Apollo's own "Verified" email status is trusted enough to auto-queue
(status='verified'); anything else (e.g. "Verifying") is flagged
'needs_manual_check', mirroring contact_discovery's verified-only-what's-actually-
verified discipline (PROJECT.md §4.3/§7) — never silently upgrade an unconfirmed
email to auto-send-eligible.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent.contact_discovery import categorize_role

SOURCE_KEY = "apollo"

REQUIRED_APOLLO_COLUMNS = {"Company Name", "Email"}

# Only Apollo's own "Verified" status is trusted to auto-queue; everything else
# (e.g. "Verifying", or a status Apollo introduces later) is conservatively flagged
# for manual review rather than guessed as good.
APOLLO_STATUS_MAP: dict[str, tuple[str, float]] = {
    "verified": ("verified", 1.0),
}
DEFAULT_STATUS: tuple[str, float] = ("needs_manual_check", 0.5)


class ApolloImportError(Exception):
    """Raised when the Apollo CSV can't be read or is missing required data."""


def extract_domain(website: str | None) -> str | None:
    """"https://hotstar.com" -> "hotstar.com"; blank/None -> None."""
    if not website:
        return None
    domain = website.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0].strip()
    return domain or None


def categorize_from_apollo(departments: str | None, title: str | None) -> str:
    """Prefers Apollo's own Departments field over title-keyword matching.

    Departments is more reliable — e.g. "Product Operations Manager" contains none
    of contact_discovery.ROLE_KEYWORDS' literal phrases ("product manager", "product
    owner", …), but Departments explicitly says "Product, Operations". Falls back to
    the existing title-keyword heuristic (categorize_role) when Departments is blank
    or doesn't mention HR/Product.
    """
    if departments:
        lowered = departments.lower()
        if "human resources" in lowered:
            return "hr"
        if "product" in lowered:
            return "product"
    return categorize_role(title)


def map_email_status(email_status: str | None) -> tuple[str, float]:
    key = (email_status or "").strip().lower()
    return APOLLO_STATUS_MAP.get(key, DEFAULT_STATUS)


def build_raw_tags(industry: str | None, keywords: str | None) -> list[str]:
    tags = [industry.strip()] if industry and industry.strip() else []
    if keywords:
        tags.extend(k.strip() for k in keywords.split(",") if k.strip())
    return tags


@dataclass(frozen=True)
class ApolloImportStats:
    rows_processed: int = 0
    companies_created: int = 0
    companies_matched: int = 0
    contacts_inserted: int = 0
    contacts_skipped_duplicate: int = 0


def _find_or_create_company(
    conn: sqlite3.Connection, name: str, domain: str | None, raw_tags: list[str]
) -> tuple[int, bool]:
    """Returns (company_id, created). Dedupes by domain when the row has one —
    matches an existing company (from Apollo or any other source) rather than
    creating a second row for the same company.
    """
    if domain:
        existing = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
        if existing is not None:
            return existing["id"], False

    cursor = conn.execute(
        "INSERT INTO companies (name, domain, source, raw_tags) VALUES (?, ?, ?, ?)",
        (name, domain, SOURCE_KEY, json.dumps(raw_tags)),
    )
    return cursor.lastrowid, True


def import_apollo_contacts(conn: sqlite3.Connection, csv_path: Path) -> ApolloImportStats:
    if not csv_path.exists():
        raise ApolloImportError(f"CSV not found: {csv_path}")

    # utf-8-sig strips a leading BOM if present (common in Apollo/Excel exports)
    # and behaves like plain utf-8 otherwise.
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ApolloImportError(f"{csv_path} has no header row")

        missing = REQUIRED_APOLLO_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ApolloImportError(
                f"{csv_path} is missing required column(s): {sorted(missing)}"
            )

        rows_processed = 0
        companies_created = 0
        companies_matched = 0
        contacts_inserted = 0
        contacts_skipped_duplicate = 0

        for line_num, row in enumerate(reader, start=2):  # header occupies line 1
            rows_processed += 1

            company_name = (row.get("Company Name") or "").strip()
            if not company_name:
                raise ApolloImportError(
                    f"{csv_path} line {line_num}: 'Company Name' is required"
                )

            email = (row.get("Email") or "").strip().lower()
            if not email:
                raise ApolloImportError(f"{csv_path} line {line_num}: 'Email' is required")

            domain = extract_domain(row.get("Website"))
            raw_tags = build_raw_tags(row.get("Industry"), row.get("Keywords"))

            company_id, created = _find_or_create_company(conn, company_name, domain, raw_tags)
            if created:
                companies_created += 1
            else:
                companies_matched += 1

            first = (row.get("First Name") or "").strip()
            last = (row.get("Last Name") or "").strip()
            contact_name = f"{first} {last}".strip() or None

            title = (row.get("Title") or "").strip() or None
            role_category = categorize_from_apollo(row.get("Departments"), title)
            status, confidence = map_email_status(row.get("Email Status"))

            try:
                conn.execute(
                    """
                    INSERT INTO contacts
                        (company_id, name, role, role_category, email,
                         verification_confidence, source_api, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        company_id,
                        contact_name,
                        title,
                        role_category,
                        email,
                        confidence,
                        SOURCE_KEY,
                        status,
                    ),
                )
                contacts_inserted += 1
            except sqlite3.IntegrityError:
                # Email already claimed by another contact row (unique index).
                contacts_skipped_duplicate += 1

    conn.commit()
    return ApolloImportStats(
        rows_processed=rows_processed,
        companies_created=companies_created,
        companies_matched=companies_matched,
        contacts_inserted=contacts_inserted,
        contacts_skipped_duplicate=contacts_skipped_duplicate,
    )
