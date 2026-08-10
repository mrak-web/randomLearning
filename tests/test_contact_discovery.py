from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Iterator

import pytest

from agent.config import load_settings
from agent.contact_discovery import (
    ContactDiscoveryError,
    DiscoveryStats,
    EmailFinder,
    FoundContact,
    HunterEmailFinder,
    categorize_role,
    discover_contacts_for_pending_companies,
    select_contact,
)
from agent.db import connect, init_db

ROLE_SEARCH_ORDER = ["hr", "product"]
MIN_CONFIDENCE = 0.7


# ---------------------------------------------------------------------------
# Fakes standing in for the real HTTP layer / a real EmailFinder
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}

    def json(self):
        return self._json_data


class FakeSession:
    """Stands in for requests.Session; records calls, returns/raises canned results."""

    def __init__(self, response_by_domain: dict):
        self._response_by_domain = response_by_domain
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        domain = params["domain"]
        result = self._response_by_domain[domain]
        if isinstance(result, Exception):
            raise result
        return result


def hunter_email_entry(
    value: str,
    confidence: int,
    first_name: str | None = "Jane",
    last_name: str | None = "Doe",
    position: str | None = "Talent Acquisition Lead",
) -> dict:
    return {
        "value": value,
        "type": "personal",
        "confidence": confidence,
        "first_name": first_name,
        "last_name": last_name,
        "position": position,
        "seniority": "senior",
        "department": "hr",
    }


class FakeEmailFinder(EmailFinder):
    source_key = "fake"

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def find_contacts(self, domain: str) -> Iterator[FoundContact]:
        self.calls.append(domain)
        result = self.responses.get(domain, [])
        if isinstance(result, Exception):
            raise result
        yield from result


def contact(
    email: str,
    confidence: float,
    role_category: str = "hr",
    name: str = "Jane Doe",
    role: str = "Talent Lead",
) -> FoundContact:
    return FoundContact(
        name=name,
        role=role,
        role_category=role_category,
        email=email,
        verification_confidence=confidence,
        source_api="fake",
    )


# ---------------------------------------------------------------------------
# categorize_role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "position,expected",
    [
        ("Talent Acquisition Lead", "hr"),
        ("HR Manager", "hr"),
        ("Senior HR Business Partner", "hr"),
        ("People Operations Manager", "hr"),
        ("Product Manager", "product"),
        ("Associate Product Manager", "product"),
        ("Co-Founder & CEO", "product"),
        ("Software Engineer", "other"),
        ("Head of Sales", "other"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_categorize_role(position, expected):
    assert categorize_role(position) == expected


def test_categorize_role_does_not_false_positive_on_substrings():
    # "Chairperson" contains no "hr"/"product" keyword as a real substring, but
    # this guards against a careless keyword list regressing that.
    assert categorize_role("Chairperson of the Board") == "other"


# ---------------------------------------------------------------------------
# select_contact
# ---------------------------------------------------------------------------


def test_select_contact_prefers_hr_over_product():
    candidates = [
        contact("pm@acme.com", 0.9, role_category="product"),
        contact("hr@acme.com", 0.6, role_category="hr"),
    ]
    chosen = select_contact(candidates, ROLE_SEARCH_ORDER)
    assert chosen.email == "hr@acme.com"


def test_select_contact_falls_back_to_product_when_no_hr():
    candidates = [contact("pm@acme.com", 0.9, role_category="product")]
    chosen = select_contact(candidates, ROLE_SEARCH_ORDER)
    assert chosen.email == "pm@acme.com"


def test_select_contact_picks_highest_confidence_within_category():
    candidates = [
        contact("hr1@acme.com", 0.5, role_category="hr"),
        contact("hr2@acme.com", 0.95, role_category="hr"),
    ]
    chosen = select_contact(candidates, ROLE_SEARCH_ORDER)
    assert chosen.email == "hr2@acme.com"


def test_select_contact_ignores_other_category():
    candidates = [contact("eng@acme.com", 0.99, role_category="other")]
    assert select_contact(candidates, ROLE_SEARCH_ORDER) is None


def test_select_contact_empty_candidates_returns_none():
    assert select_contact([], ROLE_SEARCH_ORDER) is None


# ---------------------------------------------------------------------------
# HunterEmailFinder (HTTP layer mocked — no live API calls, no quota spent)
# ---------------------------------------------------------------------------


def test_hunter_email_finder_requires_api_key():
    with pytest.raises(ContactDiscoveryError, match="API key"):
        HunterEmailFinder(api_key="")


def test_hunter_email_finder_parses_domain_search_response():
    session = FakeSession(
        {
            "acme.com": FakeResponse(
                200,
                {
                    "data": {
                        "domain": "acme.com",
                        "emails": [
                            hunter_email_entry(
                                "hr@acme.com", 92, "Priya", "Sharma", "Talent Acquisition Lead"
                            ),
                            hunter_email_entry(
                                "pm@acme.com", 65, "Rahul", "Mehta", "Product Manager"
                            ),
                        ],
                    },
                    "meta": {"results": 2},
                },
            )
        }
    )
    finder = HunterEmailFinder(api_key="key123", session=session)

    contacts = list(finder.find_contacts("acme.com"))

    assert len(contacts) == 2
    hr_contact, pm_contact = contacts
    assert hr_contact.email == "hr@acme.com"
    assert hr_contact.name == "Priya Sharma"
    assert hr_contact.role_category == "hr"
    assert hr_contact.verification_confidence == pytest.approx(0.92)
    assert pm_contact.role_category == "product"
    assert pm_contact.verification_confidence == pytest.approx(0.65)


def test_hunter_email_finder_sends_correct_request():
    session = FakeSession({"acme.com": FakeResponse(200, {"data": {"emails": []}})})
    finder = HunterEmailFinder(api_key="key123", session=session)

    list(finder.find_contacts("acme.com"))

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://api.hunter.io/v2/domain-search"
    assert call["params"] == {"domain": "acme.com", "api_key": "key123"}


def test_hunter_email_finder_skips_entries_without_value():
    session = FakeSession(
        {
            "acme.com": FakeResponse(
                200, {"data": {"emails": [{"confidence": 90, "position": "HR"}]}}
            )
        }
    )
    finder = HunterEmailFinder(api_key="key123", session=session)

    assert list(finder.find_contacts("acme.com")) == []


def test_hunter_email_finder_handles_missing_confidence():
    session = FakeSession(
        {
            "acme.com": FakeResponse(
                200,
                {"data": {"emails": [{"value": "info@acme.com", "position": None}]}},
            )
        }
    )
    finder = HunterEmailFinder(api_key="key123", session=session)

    contacts = list(finder.find_contacts("acme.com"))

    assert contacts[0].verification_confidence == 0.0
    assert contacts[0].role_category == "other"


def test_hunter_email_finder_empty_results():
    session = FakeSession({"acme.com": FakeResponse(200, {"data": {"emails": []}})})
    finder = HunterEmailFinder(api_key="key123", session=session)

    assert list(finder.find_contacts("acme.com")) == []


def test_hunter_email_finder_raises_on_non_200():
    session = FakeSession({"acme.com": FakeResponse(401, {"errors": [{"details": "bad key"}]})})
    finder = HunterEmailFinder(api_key="bad-key", session=session)

    with pytest.raises(ContactDiscoveryError, match="401"):
        list(finder.find_contacts("acme.com"))


def test_hunter_email_finder_raises_on_request_exception():
    import requests

    session = FakeSession({"acme.com": requests.ConnectionError("network down")})
    finder = HunterEmailFinder(api_key="key123", session=session)

    with pytest.raises(ContactDiscoveryError, match="request failed"):
        list(finder.find_contacts("acme.com"))


# ---------------------------------------------------------------------------
# discover_contacts_for_pending_companies (integration against a real temp DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path):
    settings = load_settings()
    settings = dataclasses.replace(settings, db_path=tmp_path / "agent.db")
    init_db(settings.db_path, settings)
    return settings.db_path


def _insert_company(conn, name, domain, niche, status) -> int:
    cur = conn.execute(
        "INSERT INTO companies (name, domain, source, niche, status) VALUES (?, ?, 'manual_csv', ?, ?)",
        (name, domain, niche, status),
    )
    return cur.lastrowid


def test_discover_contacts_only_targets_eligible_companies(db_path):
    with connect(db_path) as conn:
        consumer_id = _insert_company(conn, "Zepto", "zepto.example", "consumer", "classified")
        fintech_id = _insert_company(conn, "Razorpay", "razorpay.example", "fintech", "classified")
        no_domain_id = _insert_company(conn, "No Domain Co", None, "consumer", "classified")
        unclassified_id = _insert_company(conn, "New Co", "new.example", None, "new")
        conn.commit()

        finder = FakeEmailFinder(
            {
                "zepto.example": [contact("hr@zepto.example", 0.9)],
                "razorpay.example": [contact("hr@razorpay.example", 0.9)],
                "new.example": [contact("hr@new.example", 0.9)],
            }
        )

        stats = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )

        # Only the consumer companies (2) are eligible; fintech and 'new' status excluded.
        assert stats.companies_considered == 2
        assert stats.lookups_used == 1  # only zepto.example has a domain
        assert stats.skipped_no_domain == 1
        assert finder.calls == ["zepto.example"]

        contacts_rows = conn.execute("SELECT company_id, email FROM contacts").fetchall()
        assert len(contacts_rows) == 1
        assert contacts_rows[0]["company_id"] == consumer_id
        assert contacts_rows[0]["email"] == "hr@zepto.example"

    # Untouched companies for sanity.
    assert fintech_id and no_domain_id and unclassified_id


def test_discover_contacts_verified_vs_needs_manual_check(db_path):
    with connect(db_path) as conn:
        _insert_company(conn, "HighConfidence Co", "high.example", "consumer", "classified")
        _insert_company(conn, "LowConfidence Co", "low.example", "consumer", "classified")
        conn.commit()

        finder = FakeEmailFinder(
            {
                "high.example": [contact("hr@high.example", 0.95)],
                "low.example": [contact("hr@low.example", 0.4)],
            }
        )

        stats = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )

        assert stats.contacts_verified == 1
        assert stats.contacts_needs_manual_check == 1

        rows = {
            row["email"]: row["status"]
            for row in conn.execute("SELECT email, status FROM contacts").fetchall()
        }
        assert rows["hr@high.example"] == "verified"
        assert rows["hr@low.example"] == "needs_manual_check"


def test_discover_contacts_skips_companies_with_no_hr_or_product_contact(db_path):
    with connect(db_path) as conn:
        _insert_company(conn, "OnlyOther Co", "other.example", "consumer", "classified")
        conn.commit()

        finder = FakeEmailFinder({"other.example": [contact("eng@other.example", 0.9, role_category="other")]})

        stats = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )

        assert stats.skipped_no_contact_found == 1
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0


def test_discover_contacts_handles_lookup_errors_without_aborting(db_path):
    with connect(db_path) as conn:
        _insert_company(conn, "Broken Co", "broken.example", "consumer", "classified")
        _insert_company(conn, "Fine Co", "fine.example", "consumer", "classified")
        conn.commit()

        finder = FakeEmailFinder(
            {
                "broken.example": ContactDiscoveryError("Hunter.io 500"),
                "fine.example": [contact("hr@fine.example", 0.9)],
            }
        )

        stats = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )

        assert stats.lookup_errors == 1
        assert stats.contacts_verified == 1
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_discover_contacts_respects_max_lookups(db_path):
    with connect(db_path) as conn:
        for i in range(3):
            _insert_company(conn, f"Co{i}", f"co{i}.example", "consumer", "classified")
        conn.commit()

        finder = FakeEmailFinder({f"co{i}.example": [contact(f"hr@co{i}.example", 0.9)] for i in range(3)})

        stats = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE, max_lookups=1
        )

        assert stats.lookups_used == 1
        assert len(finder.calls) == 1
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_discover_contacts_is_safe_to_rerun(db_path):
    with connect(db_path) as conn:
        _insert_company(conn, "Zepto", "zepto.example", "consumer", "classified")
        conn.commit()

        finder = FakeEmailFinder({"zepto.example": [contact("hr@zepto.example", 0.9)]})

        first = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )
        second = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )

        assert first.contacts_verified == 1
        # Company already has a contact now, so the second run finds nothing eligible
        # and makes zero further Hunter.io calls.
        assert second.companies_considered == 0
        assert second.lookups_used == 0
        assert finder.calls == ["zepto.example"]


def test_discover_contacts_handles_duplicate_email_across_companies(db_path):
    with connect(db_path) as conn:
        other_company_id = _insert_company(conn, "Existing Co", "existing.example", "consumer", "classified")
        conn.execute(
            "INSERT INTO contacts (company_id, name, email, role_category, status) "
            "VALUES (?, 'Existing Contact', 'shared@dup.example', 'hr', 'verified')",
            (other_company_id,),
        )
        _insert_company(conn, "New Co", "newco.example", "consumer", "classified")
        conn.commit()

        finder = FakeEmailFinder({"newco.example": [contact("shared@dup.example", 0.9)]})

        stats = discover_contacts_for_pending_companies(
            conn, finder, ["consumer"], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )

        # The email collides with an existing contacts row (unique index) — treated
        # as no usable contact rather than crashing the run.
        assert stats.skipped_no_contact_found == 1
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1


def test_discover_contacts_no_active_niches_is_a_noop(db_path):
    with connect(db_path) as conn:
        _insert_company(conn, "Zepto", "zepto.example", "consumer", "classified")
        conn.commit()

        finder = FakeEmailFinder({"zepto.example": [contact("hr@zepto.example", 0.9)]})
        stats = discover_contacts_for_pending_companies(
            conn, finder, [], ROLE_SEARCH_ORDER, MIN_CONFIDENCE
        )

        assert stats == DiscoveryStats()
        assert finder.calls == []
