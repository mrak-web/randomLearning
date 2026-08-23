"""Import contacts (with their companies) from an Apollo.io People Export CSV.
See PROJECT.md §4.1/§4.3.

Usage: py scripts/import_apollo_contacts.py path/to/apollo-export.csv

Unlike scripts/import_companies_csv.py (companies only) and
scripts/discover_contacts.py (contacts via a live Hunter.io lookup), Apollo's
export already resolves a real email per contact — this creates/matches the
company row and inserts the contact directly, no Hunter.io lookup spent, and not
gated by niches.active_for_discovery.

Run scripts/classify_companies.py afterward (safe to re-run) so
scripts/generate_emails.py can pick up these contacts — only contacts whose
company has a classified niche get an email drafted.

Expected columns (Apollo's standard People Export headers): Company Name and Email
are required; First Name, Last Name, Title, Website, Industry, Keywords,
Departments, and Email Status are used when present. Extra columns are ignored.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import ApolloImportError, connect, import_apollo_contacts, init_db, load_settings


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: py scripts/import_apollo_contacts.py path/to/apollo-export.csv")
        raise SystemExit(1)

    csv_path = Path(sys.argv[1])
    settings = load_settings()
    init_db(settings.db_path, settings)

    try:
        with connect(settings.db_path) as conn:
            stats = import_apollo_contacts(conn, csv_path)
    except ApolloImportError as exc:
        print(f"Import failed: {exc}")
        raise SystemExit(1)

    print(f"Processed {stats.rows_processed} row(s) from {csv_path}")
    print(f"Companies: {stats.companies_created} created, {stats.companies_matched} matched existing")
    print(f"Contacts: {stats.contacts_inserted} inserted")
    if stats.contacts_skipped_duplicate:
        print(f"Skipped {stats.contacts_skipped_duplicate} duplicate email(s) already in the DB")
    print("Next: py scripts/classify_companies.py, then py scripts/generate_emails.py")


if __name__ == "__main__":
    main()
