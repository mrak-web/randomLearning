"""Classify pending companies by niche. See PROJECT.md §4.2.

Usage: py scripts/classify_companies.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import classify_pending_companies, connect, init_db, load_niche_keywords, load_settings


def main() -> None:
    settings = load_settings()
    init_db(settings.db_path, settings)
    niche_keywords = load_niche_keywords(settings.niche_keywords_path)

    with connect(settings.db_path) as conn:
        stats = classify_pending_companies(conn, niche_keywords, settings.niches.order)

    print(f"Classified {stats.classified} companies")
    for niche, count in stats.by_niche.items():
        if count:
            print(f"  {niche}: {count}")
    if stats.unclassifiable:
        print(f"{stats.unclassifiable} companies unclassifiable, status='skipped', needs manual tagging")


if __name__ == "__main__":
    main()
