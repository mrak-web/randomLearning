from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agent.apollo_import import (
    ApolloImportError,
    build_raw_tags,
    categorize_from_apollo,
    extract_domain,
    import_apollo_contacts,
    map_email_status,
)
from agent.config import load_settings
from agent.db import connect, init_db

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = REPO_ROOT / "tests" / "fixtures" / "apollo_contacts_sample.csv"


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "website,expected",
    [
        ("https://hotstar.com", "hotstar.com"),
        ("http://zomato.com", "zomato.com"),
        ("https://Pixxel.Space/careers", "pixxel.space"),
        ("", None),
        (None, None),
    ],
)
def test_extract_domain(website, expected):
    assert extract_domain(website) == expected


def test_categorize_from_apollo_prefers_departments_hr():
    assert categorize_from_apollo("Human Resources", "Recruiter") == "hr"


def test_categorize_from_apollo_prefers_departments_product():
    # Real example: title-keyword matching alone would miss this (no literal
    # "product manager"/"product owner"/etc. phrase), but Departments says "Product".
    assert categorize_from_apollo("Product, Operations", "Product Operations Manager") == "product"


def test_categorize_from_apollo_falls_back_to_title_when_departments_blank():
    assert categorize_from_apollo(None, "Product Manager") == "product"
    assert categorize_from_apollo("", "Talent Acquisition Lead") == "hr"


def test_categorize_from_apollo_falls_back_to_other():
    assert categorize_from_apollo("Engineering", "Software Engineer") == "other"


def test_map_email_status_verified():
    assert map_email_status("Verified") == ("verified", 1.0)
    assert map_email_status("verified") == ("verified", 1.0)  # case-insensitive


def test_map_email_status_anything_else_is_manual_check():
    assert map_email_status("Verifying") == ("needs_manual_check", 0.5)
    assert map_email_status("Unverified") == ("needs_manual_check", 0.5)
    assert map_email_status(None) == ("needs_manual_check", 0.5)
    assert map_email_status("") == ("needs_manual_check", 0.5)


def test_build_raw_tags_combines_industry_and_keywords():
    tags = build_raw_tags("entertainment", "ecommerce, d2c, b2c")
    assert tags == ["entertainment", "ecommerce", "d2c", "b2c"]


def test_build_raw_tags_handles_missing_fields():
    assert build_raw_tags(None, None) == []
    assert build_raw_tags("entertainment", None) == ["entertainment"]
    assert build_raw_tags(None, "ecommerce") == ["ecommerce"]


# ---------------------------------------------------------------------------
# import_apollo_contacts (DB integration)
# ---------------------------------------------------------------------------


def test_import_apollo_contacts_dedupes_company_by_domain(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        stats = import_apollo_contacts(conn, SAMPLE_CSV)

    # 5 rows: 2 JioHotstar rows share a domain -> 1 company created for both,
    # Zomato, Pixxel, District each get their own -> 4 companies total.
    assert stats.rows_processed == 5
    assert stats.companies_created == 4
    assert stats.companies_matched == 1  # the second JioHotstar row
    assert stats.contacts_inserted == 5
    assert stats.contacts_skipped_duplicate == 0


def test_import_apollo_contacts_persists_expected_fields(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        import_apollo_contacts(conn, SAMPLE_CSV)

        company = conn.execute(
            "SELECT name, domain, source, status FROM companies WHERE domain = 'hotstar.com'"
        ).fetchone()
        contacts = conn.execute(
            """
            SELECT ct.name, ct.role, ct.role_category, ct.email, ct.status,
                   ct.verification_confidence, ct.source_api
            FROM contacts ct
            JOIN companies co ON co.id = ct.company_id
            WHERE co.domain = 'hotstar.com'
            ORDER BY ct.email
            """
        ).fetchall()

    assert company["name"] == "JioHotstar"
    assert company["source"] == "apollo"
    assert company["status"] == "new"  # not classified by this importer

    assert len(contacts) == 2
    vaibhav = contacts[1]
    assert vaibhav["name"] == "Vaibhav Haseja"
    assert vaibhav["role"] == "Product Manager"
    assert vaibhav["role_category"] == "product"
    assert vaibhav["status"] == "verified"
    assert vaibhav["verification_confidence"] == 1.0
    assert vaibhav["source_api"] == "apollo"


def test_import_apollo_contacts_maps_verifying_status_to_manual_check(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        import_apollo_contacts(conn, SAMPLE_CSV)
        row = conn.execute(
            "SELECT status, verification_confidence FROM contacts WHERE email = 'samarth.ghule@pixxel.co.in'"
        ).fetchone()

    assert row["status"] == "needs_manual_check"
    assert row["verification_confidence"] == 0.5


def test_import_apollo_contacts_different_company_same_email_domain(settings):
    # Real-data edge case: District's contact uses a @zomato.com email address, but
    # District's own Website (district.in) means it must NOT be merged into the
    # Zomato company row -- domain matching uses the Website column, not the email.
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        import_apollo_contacts(conn, SAMPLE_CSV)
        district = conn.execute(
            "SELECT id, domain FROM companies WHERE name = 'District'"
        ).fetchone()
        zomato = conn.execute(
            "SELECT id, domain FROM companies WHERE name = 'Zomato'"
        ).fetchone()

    assert district["domain"] == "district.in"
    assert zomato["domain"] == "zomato.com"
    assert district["id"] != zomato["id"]


def test_import_apollo_contacts_role_category_uses_departments_over_title(settings):
    # "Product Operations Manager" has no literal ROLE_KEYWORDS phrase, but
    # Departments says "Product, Operations".
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        import_apollo_contacts(conn, SAMPLE_CSV)
        row = conn.execute(
            "SELECT role_category FROM contacts WHERE email = 'shivang.garg@zomato.com'"
        ).fetchone()

    assert row["role_category"] == "product"


def test_import_apollo_contacts_is_safe_to_rerun(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        first = import_apollo_contacts(conn, SAMPLE_CSV)
        second = import_apollo_contacts(conn, SAMPLE_CSV)

    assert first.contacts_inserted == 5
    assert second.contacts_inserted == 0
    assert second.contacts_skipped_duplicate == 5
    # Companies match existing (by domain) on rerun rather than duplicating --
    # companies_matched counts per row (all 5), not per unique company.
    assert second.companies_created == 0
    assert second.companies_matched == 5


def test_import_apollo_contacts_missing_file_raises(settings):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        with pytest.raises(ApolloImportError, match="not found"):
            import_apollo_contacts(conn, settings.db_path.parent / "nope.csv")


def test_import_apollo_contacts_missing_required_column_raises(settings, tmp_path: Path):
    init_db(settings.db_path, settings)
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("First Name,Title\nJane,PM\n", encoding="utf-8")

    with connect(settings.db_path) as conn:
        with pytest.raises(ApolloImportError, match="Company Name"):
            import_apollo_contacts(conn, bad_csv)


def test_import_apollo_contacts_blank_email_raises(settings, tmp_path: Path):
    init_db(settings.db_path, settings)
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("Company Name,Email\nAcme,\n", encoding="utf-8")

    with connect(settings.db_path) as conn:
        with pytest.raises(ApolloImportError, match="line 2"):
            import_apollo_contacts(conn, bad_csv)


def test_import_apollo_contacts_blank_company_name_raises(settings, tmp_path: Path):
    init_db(settings.db_path, settings)
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("Company Name,Email\n,jane@acme.com\n", encoding="utf-8")

    with connect(settings.db_path) as conn:
        with pytest.raises(ApolloImportError, match="Company Name"):
            import_apollo_contacts(conn, bad_csv)
