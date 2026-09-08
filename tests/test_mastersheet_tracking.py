from __future__ import annotations

from datetime import date
from pathlib import Path

import openpyxl
import pytest

from agent.mastersheet_tracking import (
    CANDIDATES_SHEET,
    read_raw_candidates,
    sync_pipeline_candidates,
)


def _build_mastersheet(path: Path, raw_rows: list[tuple]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "raw"
    ws.append(["Company", "Name", "Designation", "E-Mail 1", "Email 2", "Notes"])
    for row in raw_rows:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def mastersheet_path(tmp_path: Path) -> Path:
    return tmp_path / "Email Mastersheet.xlsx"


def test_read_raw_candidates_skips_rows_with_no_company_or_no_email(mastersheet_path):
    _build_mastersheet(
        mastersheet_path,
        [
            ("Acme", "Jane Doe", "PM", "jane@acme.com", None, "a note"),
            ("", "No Company", "PM", "noco@example.com", None, None),  # no company
            ("NoEmail Inc", "No Email", "PM", None, None, None),  # no email at all
            ("Fallback Co", "Uses Email2", "PM", None, "fallback@fallback.com", None),
        ],
    )
    wb = openpyxl.load_workbook(mastersheet_path)

    candidates = read_raw_candidates(wb)

    emails = {c.email for c in candidates}
    assert emails == {"jane@acme.com", "fallback@fallback.com"}


def test_sync_pipeline_candidates_creates_sheet_and_adds_new_rows(mastersheet_path):
    _build_mastersheet(
        mastersheet_path,
        [("Acme", "Jane Doe", "PM", "jane@acme.com", None, None)],
    )

    stats = sync_pipeline_candidates(
        mastersheet_path, existing_domains=set(), existing_names=set(), today=date(2026, 9, 9)
    )

    assert stats.candidates_found == 1
    assert stats.newly_tracked == 1
    assert stats.already_in_pipeline_db == 0
    assert stats.already_tracked == 0

    wb = openpyxl.load_workbook(mastersheet_path)
    assert CANDIDATES_SHEET in wb.sheetnames
    rows = list(wb[CANDIDATES_SHEET].iter_rows(min_row=2, values_only=True))
    assert rows == [("Acme", "Jane Doe", "PM", "jane@acme.com", "acme.com", "New", "2026-09-09", None)]


def test_sync_pipeline_candidates_skips_company_already_in_agent_db_by_domain(mastersheet_path):
    _build_mastersheet(
        mastersheet_path,
        [("Acme", "Jane Doe", "PM", "jane@acme.com", None, None)],
    )

    stats = sync_pipeline_candidates(
        mastersheet_path, existing_domains={"acme.com"}, existing_names=set()
    )

    assert stats.already_in_pipeline_db == 1
    assert stats.newly_tracked == 0


def test_sync_pipeline_candidates_skips_company_already_in_agent_db_by_name_fallback(mastersheet_path):
    """Some agent.db companies have no domain (e.g. hand-entered test rows) -- name
    match is the fallback dedup path for those."""
    _build_mastersheet(
        mastersheet_path,
        [("No Domain Co", "Jane Doe", "PM", "jane@nodomainco.com", None, None)],
    )

    stats = sync_pipeline_candidates(
        mastersheet_path, existing_domains=set(), existing_names={"no domain co"}
    )

    assert stats.already_in_pipeline_db == 1
    assert stats.newly_tracked == 0


def test_sync_pipeline_candidates_is_idempotent_across_reruns(mastersheet_path):
    _build_mastersheet(
        mastersheet_path,
        [("Acme", "Jane Doe", "PM", "jane@acme.com", None, None)],
    )

    first = sync_pipeline_candidates(mastersheet_path, existing_domains=set(), existing_names=set())
    second = sync_pipeline_candidates(mastersheet_path, existing_domains=set(), existing_names=set())

    assert first.newly_tracked == 1
    assert second.newly_tracked == 0
    assert second.already_tracked == 1

    wb = openpyxl.load_workbook(mastersheet_path)
    rows = list(wb[CANDIDATES_SHEET].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 1  # not duplicated


def test_sync_pipeline_candidates_leaves_other_sheets_untouched(mastersheet_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "raw"
    ws.append(["Company", "Name", "Designation", "E-Mail 1", "Email 2", "Notes"])
    ws.append(("Acme", "Jane Doe", "PM", "jane@acme.com", None, None))
    company_sheet = wb.create_sheet("Company")
    company_sheet.append(["Company Name", None, "Status", "Comments"])
    company_sheet.append(["Air India", None, "Did Not Reply", None])
    wb.save(mastersheet_path)

    sync_pipeline_candidates(mastersheet_path, existing_domains=set(), existing_names=set())

    wb = openpyxl.load_workbook(mastersheet_path)
    company_rows = list(wb["Company"].iter_rows(min_row=2, values_only=True))
    assert company_rows == [("Air India", None, "Did Not Reply", None)]
