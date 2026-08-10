"""Find contacts for classified, in-scope companies via Hunter.io. See PROJECT.md §4.3.

Usage: py scripts/discover_contacts.py [--max-lookups N]

Requires the HUNTER_API_KEY environment variable (never stored in settings.yaml or
committed to the repo). Only runs against companies whose niche is in
niches.active_for_discovery (settings.yaml) — currently consumer only.

--max-lookups caps the number of live Hunter.io API calls made in this single run
(default 10), as a safety net against burning free-tier quota (25/month) on one run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import HunterEmailFinder, connect, discover_contacts_for_pending_companies, init_db, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-lookups",
        type=int,
        default=10,
        help="Cap on live Hunter.io API calls this run (default: 10)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("HUNTER_API_KEY")
    if not api_key:
        print("HUNTER_API_KEY is not set. Get a free-tier key at hunter.io and run:")
        print("  set HUNTER_API_KEY=your-key-here   (Windows cmd)")
        print("  $env:HUNTER_API_KEY = 'your-key-here'   (PowerShell)")
        raise SystemExit(1)

    settings = load_settings()
    init_db(settings.db_path, settings)
    finder = HunterEmailFinder(api_key=api_key)

    with connect(settings.db_path) as conn:
        stats = discover_contacts_for_pending_companies(
            conn,
            finder,
            active_niches=settings.niches.active_for_discovery,
            role_search_order=settings.contact_discovery.role_search_order,
            min_verification_confidence=settings.contact_discovery.min_verification_confidence,
            max_lookups=args.max_lookups,
        )

    print(f"Considered {stats.companies_considered} eligible companies")
    print(f"Used {stats.lookups_used} Hunter.io lookup(s)")
    print(f"Verified contacts: {stats.contacts_verified}")
    print(f"Needs manual check: {stats.contacts_needs_manual_check}")
    if stats.skipped_no_domain:
        print(f"Skipped (no domain): {stats.skipped_no_domain}")
    if stats.skipped_no_contact_found:
        print(f"Skipped (no hr/product contact found): {stats.skipped_no_contact_found}")
    if stats.lookup_errors:
        print(f"Lookup errors: {stats.lookup_errors}")


if __name__ == "__main__":
    main()
