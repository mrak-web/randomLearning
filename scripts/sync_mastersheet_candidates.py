"""Pull new candidates from data/Email Mastersheet.xlsx's 'raw' sheet into a
'Pipeline Candidates' tracking sheet in that same workbook -- kept deliberately
separate from agent.db / the Streamlit dashboard (Arjun's decision, 2026-09-09), not a
new source feeding the automated pipeline. Only dedupes against companies already in
agent.db; the mastersheet's own Company/Outreach/useless sheets are a friend's
reference template, not Arjun's real history, and are never read or written.

Usage: py scripts/sync_mastersheet_candidates.py
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import connect, load_settings
from agent.mastersheet_tracking import sync_pipeline_candidates

MASTERSHEET_PATH = Path("data/Email Mastersheet.xlsx")


def main() -> None:
    if not MASTERSHEET_PATH.exists():
        print(f"Mastersheet not found at {MASTERSHEET_PATH}")
        raise SystemExit(1)

    settings = load_settings()

    # This writes to Arjun's real spreadsheet -- back it up first since openpyxl
    # resaving the whole workbook is not something to risk without a rollback copy.
    backup_path = MASTERSHEET_PATH.with_name(
        f"{MASTERSHEET_PATH.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{MASTERSHEET_PATH.suffix}"
    )
    shutil.copy2(MASTERSHEET_PATH, backup_path)

    with connect(settings.db_path) as conn:
        existing_domains = {
            row[0] for row in conn.execute("SELECT domain FROM companies WHERE domain IS NOT NULL")
        }
        existing_names = {row[0] for row in conn.execute("SELECT name FROM companies")}

    stats = sync_pipeline_candidates(MASTERSHEET_PATH, existing_domains, existing_names)

    print(f"Backup saved to {backup_path}")
    print(f"'raw' sheet candidates found: {stats.candidates_found}")
    print(f"Already in agent.db (skipped): {stats.already_in_pipeline_db}")
    print(f"Already tracked from a previous run (skipped): {stats.already_tracked}")
    print(f"Newly added to '{'Pipeline Candidates'}': {stats.newly_tracked}")


if __name__ == "__main__":
    main()
