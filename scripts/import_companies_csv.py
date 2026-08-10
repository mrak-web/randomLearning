"""Import companies from a manually curated CSV. See PROJECT.md §4.1.

Usage: py scripts/import_companies_csv.py path/to/companies.csv

Expected CSV columns: name (required), domain (optional), tags (optional,
semicolon-separated, e.g. "marketplace;ride-hailing").
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import ManualCsvSource, SourcingError, connect, import_companies, init_db, load_settings


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: py scripts/import_companies_csv.py path/to/companies.csv")
        raise SystemExit(1)

    csv_path = Path(sys.argv[1])
    settings = load_settings()
    init_db(settings.db_path, settings)

    source = ManualCsvSource(csv_path)
    try:
        with connect(settings.db_path) as conn:
            stats = import_companies(conn, source.fetch())
    except SourcingError as exc:
        print(f"Import failed: {exc}")
        raise SystemExit(1)

    print(f"Imported {stats.inserted} companies from {csv_path}")
    if stats.skipped_duplicate:
        print(f"Skipped {stats.skipped_duplicate} duplicate domain(s) already in the DB")


if __name__ == "__main__":
    main()
