"""Combines the two LinkedIn lookup sources (agent/linkedin_jobs.py,
agent/linkedin_posts.py) into one workbook. See PROJECT.md §8.1.

Deliberately two sheets, not one merged table — job listings and hiring posts have
genuinely different shapes (structured title/company/link vs. free-text author/
excerpt), so forcing them into shared columns would mean misleading blanks or guessed
values on one side or the other.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from agent.linkedin_jobs import LinkedInJobPosting, populate_jobs_sheet
from agent.linkedin_posts import HiringPost, populate_hiring_posts_sheet


def write_linkedin_excel(
    job_postings: list[LinkedInJobPosting],
    hiring_posts: list[HiringPost],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    jobs_sheet = workbook.active
    jobs_sheet.title = "LinkedIn Jobs"
    populate_jobs_sheet(jobs_sheet, job_postings)

    posts_sheet = workbook.create_sheet("Hiring Posts")
    populate_hiring_posts_sheet(posts_sheet, hiring_posts)

    workbook.save(output_path)
