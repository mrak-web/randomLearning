from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from agent.config import load_settings
from agent.db import connect, init_db
from agent.sourcing import ManualCsvSource, RawCompany, SourcingError, import_companies

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = REPO_ROOT / "tests" / "fixtures" / "companies_sample.csv"


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


def test_manual_csv_source_parses_sample_fixture():
    companies = list(ManualCsvSource(SAMPLE_CSV).fetch())

    assert len(companies) == 6
    zepto = companies[0]
    assert zepto == RawCompany(
        name="Zepto",
        domain="zepto.co.in",
        source="manual_csv",
        raw_tags=["consumer", "quick-commerce"],
    )


def test_manual_csv_source_lowercases_domain(tmp_path: Path):
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,domain\nAcme,ACME.COM\n", encoding="utf-8")

    companies = list(ManualCsvSource(csv_path).fetch())

    assert companies[0].domain == "acme.com"


def test_manual_csv_source_blank_domain_becomes_none(tmp_path: Path):
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,domain\nAcme,\n", encoding="utf-8")

    companies = list(ManualCsvSource(csv_path).fetch())

    assert companies[0].domain is None


def test_manual_csv_source_missing_tags_column_is_fine(tmp_path: Path):
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,domain\nAcme,acme.com\n", encoding="utf-8")

    companies = list(ManualCsvSource(csv_path).fetch())

    assert companies[0].raw_tags == []


def test_manual_csv_source_missing_file_raises(tmp_path: Path):
    with pytest.raises(SourcingError, match="not found"):
        list(ManualCsvSource(tmp_path / "nope.csv").fetch())


def test_manual_csv_source_missing_required_column_raises(tmp_path: Path):
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("domain,tags\nacme.com,x\n", encoding="utf-8")

    with pytest.raises(SourcingError, match="name"):
        list(ManualCsvSource(csv_path).fetch())


def test_manual_csv_source_blank_name_raises(tmp_path: Path):
    csv_path = tmp_path / "companies.csv"
    csv_path.write_text("name,domain\n,acme.com\n", encoding="utf-8")

    with pytest.raises(SourcingError, match="line 2"):
        list(ManualCsvSource(csv_path).fetch())


def test_import_companies_inserts_and_dedupes(settings):
    init_db(settings.db_path, settings)
    companies = list(ManualCsvSource(SAMPLE_CSV).fetch())

    with connect(settings.db_path) as conn:
        stats = import_companies(conn, companies)

    # 6 rows in the fixture, 1 is a domain duplicate of an earlier row.
    assert stats.inserted == 5
    assert stats.skipped_duplicate == 1


def test_import_companies_persists_rows_with_expected_fields(settings):
    init_db(settings.db_path, settings)
    companies = list(ManualCsvSource(SAMPLE_CSV).fetch())

    with connect(settings.db_path) as conn:
        import_companies(conn, companies)
        row = conn.execute(
            "SELECT name, domain, source, raw_tags, status FROM companies WHERE domain = 'zepto.co.in'"
        ).fetchone()

    assert row["name"] == "Zepto"
    assert row["source"] == "manual_csv"
    assert row["status"] == "new"
    assert "quick-commerce" in row["raw_tags"]


def test_import_companies_allows_multiple_null_domains(settings):
    init_db(settings.db_path, settings)
    companies = list(ManualCsvSource(SAMPLE_CSV).fetch())

    with connect(settings.db_path) as conn:
        stats = import_companies(conn, companies)
        null_domain_count = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE domain IS NULL"
        ).fetchone()[0]

    assert null_domain_count == 2
    assert stats.inserted + stats.skipped_duplicate == len(companies)


def test_import_companies_is_safe_to_rerun(settings):
    init_db(settings.db_path, settings)
    companies = list(ManualCsvSource(SAMPLE_CSV).fetch())

    with connect(settings.db_path) as conn:
        first = import_companies(conn, companies)
        second = import_companies(conn, companies)

    assert first.inserted == 5
    # Null-domain rows have no uniqueness constraint, so they re-insert on rerun;
    # only the 4 domain-bearing rows are caught as duplicates the second time.
    assert second.inserted == 2
    assert second.skipped_duplicate == 4
