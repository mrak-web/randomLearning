"""Contact discovery for the cold-email job agent. See PROJECT.md §4.3.

EmailFinder is a thin, swappable interface (Hunter.io today; Apollo/Prospeo/Snov.io
could slot in later without touching the orchestration logic in
discover_contacts_for_pending_companies). No LinkedIn scraping, no guessed
first.last@domain.com addresses — only verified/deliverable-confidence results from
a licensed finder API are ever queued.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

import requests

HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"

# Position-text substrings (case-insensitive) that categorize a Hunter contact.
# Checked in this order — a position matching both would count as whichever key
# is listed first, though "hr"/"product" phrasing rarely overlaps in practice.
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "hr": (
        "human resources",
        "talent",
        "recruit",
        "people ops",
        "people operations",
        "hiring",
        " hr",
        "hr ",
    ),
    "product": (
        "product manager",
        "product owner",
        "product lead",
        "apm",
        "associate product manager",
        "head of product",
        "vp of product",
        "founder",
        "co-founder",
        "cofounder",
        "ceo",
    ),
}


class ContactDiscoveryError(Exception):
    """Raised when a finder API call fails or can't be used."""


def categorize_role(position: str | None) -> str:
    if not position:
        return "other"
    lowered = position.lower()
    for category, keywords in ROLE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return "other"


@dataclass(frozen=True)
class FoundContact:
    name: str | None
    role: str | None
    role_category: str
    email: str
    verification_confidence: float  # normalized 0.0-1.0
    source_api: str


class EmailFinder(ABC):
    """A pluggable contact-discovery source, keyed to contacts.source_api in the schema."""

    source_key: str

    @abstractmethod
    def find_contacts(self, domain: str) -> Iterator[FoundContact]:
        ...


class HunterEmailFinder(EmailFinder):
    """Hunter.io Domain Search (https://hunter.io/api-documentation/v2#domain-search)."""

    source_key = "hunter"

    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ):
        if not api_key:
            raise ContactDiscoveryError("Hunter.io API key is required")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    def find_contacts(self, domain: str) -> Iterator[FoundContact]:
        try:
            response = self.session.get(
                HUNTER_DOMAIN_SEARCH_URL,
                params={"domain": domain, "api_key": self.api_key},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ContactDiscoveryError(f"Hunter.io request failed for {domain}: {exc}") from exc

        if response.status_code != 200:
            raise ContactDiscoveryError(
                f"Hunter.io domain search failed for {domain}: HTTP {response.status_code}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ContactDiscoveryError(
                f"Hunter.io returned non-JSON response for {domain}"
            ) from exc

        emails = (body.get("data") or {}).get("emails") or []
        for entry in emails:
            email = entry.get("value")
            if not email:
                continue

            confidence_raw = entry.get("confidence")
            confidence = (confidence_raw / 100.0) if confidence_raw is not None else 0.0

            first = entry.get("first_name") or ""
            last = entry.get("last_name") or ""
            name = f"{first} {last}".strip() or None

            position = entry.get("position")

            yield FoundContact(
                name=name,
                role=position,
                role_category=categorize_role(position),
                email=email,
                verification_confidence=confidence,
                source_api=self.source_key,
            )


def select_contact(
    candidates: list[FoundContact], role_search_order: list[str]
) -> FoundContact | None:
    """Picks the best candidate per role_search_order (e.g. hr tried before product).

    Within a category, the highest-confidence candidate wins. Categories not listed
    in role_search_order (e.g. "other") are never selected — only role types the
    caller explicitly opted into are acceptable outcomes.
    """
    by_category: dict[str, list[FoundContact]] = {}
    for candidate in candidates:
        by_category.setdefault(candidate.role_category, []).append(candidate)

    for category in role_search_order:
        pool = by_category.get(category)
        if pool:
            return max(pool, key=lambda c: c.verification_confidence)
    return None


@dataclass(frozen=True)
class DiscoveryStats:
    companies_considered: int = 0
    lookups_used: int = 0
    contacts_verified: int = 0
    contacts_needs_manual_check: int = 0
    skipped_no_domain: int = 0
    skipped_no_contact_found: int = 0
    lookup_errors: int = 0


def discover_contacts_for_pending_companies(
    conn: sqlite3.Connection,
    finder: EmailFinder,
    active_niches: list[str],
    role_search_order: list[str],
    min_verification_confidence: float,
    max_lookups: int | None = None,
) -> DiscoveryStats:
    """Finds a contact for each eligible company, skipping ones that already have one.

    Eligible = status='classified', niche in active_niches, no existing contacts row.
    max_lookups caps the number of live finder API calls made in this run (not the
    number of companies considered) — a safety cap against burning free-tier quota.
    """
    if not active_niches:
        return DiscoveryStats()

    placeholders = ",".join("?" for _ in active_niches)
    rows = conn.execute(
        f"""
        SELECT c.id, c.domain
        FROM companies c
        LEFT JOIN contacts ct ON ct.company_id = c.id
        WHERE c.status = 'classified'
          AND c.niche IN ({placeholders})
          AND ct.id IS NULL
        """,
        active_niches,
    ).fetchall()

    companies_considered = 0
    lookups_used = 0
    contacts_verified = 0
    contacts_needs_manual_check = 0
    skipped_no_domain = 0
    skipped_no_contact_found = 0
    lookup_errors = 0

    for row in rows:
        companies_considered += 1

        if not row["domain"]:
            skipped_no_domain += 1
            continue

        if max_lookups is not None and lookups_used >= max_lookups:
            break

        lookups_used += 1
        try:
            candidates = list(finder.find_contacts(row["domain"]))
        except ContactDiscoveryError:
            lookup_errors += 1
            continue

        contact = select_contact(candidates, role_search_order)
        if contact is None:
            skipped_no_contact_found += 1
            continue

        status = (
            "verified"
            if contact.verification_confidence >= min_verification_confidence
            else "needs_manual_check"
        )
        try:
            conn.execute(
                """
                INSERT INTO contacts
                    (company_id, name, role, role_category, email,
                     verification_confidence, source_api, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    contact.name,
                    contact.role,
                    contact.role_category,
                    contact.email,
                    contact.verification_confidence,
                    contact.source_api,
                    status,
                ),
            )
        except sqlite3.IntegrityError:
            # Email already claimed by another contact row (unique index) — nothing
            # usable came out of this lookup.
            skipped_no_contact_found += 1
            continue

        if status == "verified":
            contacts_verified += 1
        else:
            contacts_needs_manual_check += 1

    conn.commit()
    return DiscoveryStats(
        companies_considered=companies_considered,
        lookups_used=lookups_used,
        contacts_verified=contacts_verified,
        contacts_needs_manual_check=contacts_needs_manual_check,
        skipped_no_domain=skipped_no_domain,
        skipped_no_contact_found=skipped_no_contact_found,
        lookup_errors=lookup_errors,
    )
