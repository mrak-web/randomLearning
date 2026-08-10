"""Generate pending-review email drafts for verified contacts. See PROJECT.md §4.4.

Usage: py scripts/generate_emails.py

Only writes drafts to email_queue (status='pending_review') — nothing here sends
anything or touches the Gmail API.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import connect, generate_pending_emails, init_db, load_settings, load_story_bank


def main() -> None:
    settings = load_settings()
    init_db(settings.db_path, settings)
    story_bank = load_story_bank(settings.story_bank_path)

    if not settings.resume_pdf_path.exists():
        print(f"Warning: resume PDF not found at {settings.resume_pdf_path}.")
        print("Drafts will still record attached_resume=True per settings, but the")
        print("actual PDF needs to exist before module 7 (sending) can attach it.\n")

    with connect(settings.db_path) as conn:
        stats = generate_pending_emails(
            conn,
            story_bank,
            settings.templates_dir,
            settings.sender_display_name,
            settings.attach_resume_by_default,
        )

    print(f"Generated {stats.generated} draft(s) with status=pending_review")
    if stats.skipped_no_niche:
        print(f"Skipped (no niche or story-bank entry): {stats.skipped_no_niche}")
    if stats.skipped_no_template:
        print(f"Skipped (no template file for niche): {stats.skipped_no_template}")


if __name__ == "__main__":
    main()
