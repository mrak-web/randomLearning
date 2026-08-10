"""Company sourcing for the cold-email job agent. See PROJECT.md §4.1.

Each source is a small class implementing CompanySource.fetch() -> Iterator[RawCompany],
mirroring the swappable-interface pattern used for EmailFinder (§4.2). ManualCsvSource is
the only source implemented so far; YC/ProductHunt/Startup India pull from live third-party
APIs and are a separate follow-up once their response shapes are verified against the real
endpoints.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

REQUIRED_CSV_COLUMNS = {"name"}


class SourcingError(Exception):
    """Raised when a company source can't produce valid data."""


@dataclass(frozen=True)
class RawCompany:
    name: str
    domain: str | None
    source: str
    raw_tags: list[str] = field(default_factory=list)


class CompanySource(ABC):
    """A pluggable source of candidate companies, keyed to companies.source in the schema."""

    source_key: str

    @abstractmethod
    def fetch(self) -> Iterator[RawCompany]:
        ...


class ManualCsvSource(CompanySource):
    """Reads companies from a hand-curated CSV — the hatch described in PROJECT.md §4.1.

    Expected columns:
      name    (required) — company name
      domain  (optional) — lowercased, used for de-duplication against existing rows
      tags    (optional) — semicolon-separated, e.g. "marketplace;ride-hailing"
    """

    source_key = "manual_csv"

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def fetch(self) -> Iterator[RawCompany]:
        if not self.csv_path.exists():
            raise SourcingError(f"CSV not found: {self.csv_path}")

        with self.csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise SourcingError(f"{self.csv_path} has no header row")

            missing = REQUIRED_CSV_COLUMNS - set(reader.fieldnames)
            if missing:
                raise SourcingError(
                    f"{self.csv_path} is missing required column(s): {sorted(missing)}"
                )

            for line_num, row in enumerate(reader, start=2):  # header occupies line 1
                name = (row.get("name") or "").strip()
                if not name:
                    raise SourcingError(f"{self.csv_path} line {line_num}: 'name' is required")

                domain = (row.get("domain") or "").strip().lower() or None

                tags_raw = (row.get("tags") or "").strip()
                tags = [t.strip() for t in tags_raw.split(";") if t.strip()] if tags_raw else []

                yield RawCompany(name=name, domain=domain, source=self.source_key, raw_tags=tags)


@dataclass(frozen=True)
class ImportStats:
    inserted: int
    skipped_duplicate: int


def import_companies(conn: sqlite3.Connection, companies: Iterable[RawCompany]) -> ImportStats:
    """Insert RawCompany rows into the companies table, skipping domain duplicates."""
    inserted = 0
    skipped_duplicate = 0
    for company in companies:
        try:
            conn.execute(
                "INSERT INTO companies (name, domain, source, raw_tags) VALUES (?, ?, ?, ?)",
                (company.name, company.domain, company.source, json.dumps(company.raw_tags)),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped_duplicate += 1
    conn.commit()
    return ImportStats(inserted=inserted, skipped_duplicate=skipped_duplicate)
