"""Sanity-check entry point for module 1: initializes agent.db and reports table state.

Usage: py scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import init_db, load_settings, load_story_bank, missing_templates, table_counts


def main() -> None:
    settings = load_settings()
    init_db(settings.db_path, settings)
    counts = table_counts(settings.db_path)

    print(f"Initialized DB at {settings.db_path}")
    for table, count in counts.items():
        print(f"  {table}: {count} rows")

    story_bank = load_story_bank()
    missing = missing_templates(settings, story_bank)
    print(f"\nNiches: {', '.join(settings.niches.order)}")
    print(f"Active for contact discovery: {', '.join(settings.niches.active_for_discovery)}")
    if missing:
        print(f"Templates not yet written for: {', '.join(missing)}")


if __name__ == "__main__":
    main()
