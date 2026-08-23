"""Search LinkedIn job postings AND hiring posts (public, logged-out) and export
them to a single Excel workbook.

Usage: py scripts/find_linkedin_jobs.py [--output PATH]

Requires the APIFY_API_TOKEN environment variable (never stored in settings.yaml or
committed to the repo — same pattern as HUNTER_API_KEY). Search keywords, location,
date windows, and result caps all come from settings.yaml's linkedin_jobs and
linkedin_posts sections.

This is a standalone research tool: it never touches Arjun's own LinkedIn login (both
Actors scrape LinkedIn's public, logged-out pages), and results are NOT written to
agent.db or fed into the cold-email send pipeline — the Excel file is the whole
deliverable. Arjun acts on these leads manually, referencing the specific posting/post.

The output workbook has two sheets:
  - "LinkedIn Jobs": structured listings from LinkedIn's formal Jobs tab, scored and
    sorted against Arjun's resume profile (config/resume_profile.yaml) — a rule-based
    match score, not an LLM call, so this costs nothing beyond the Apify scrape (see
    agent/resume_match.py).
  - "Hiring Posts": PM/HR people personally announcing a hiring need in a regular
    LinkedIn post — noisier, free-text data, already filtered for relevance and
    seniority (see agent/linkedin_posts.py's filter_relevant_posts) but still worth
    a human skim.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import (
    ApifyLinkedInJobSearch,
    ApifyLinkedInPostSearch,
    attach_match_scores,
    build_post_search_queries,
    filter_relevant_posts,
    load_niche_keywords,
    load_resume_profile,
    load_settings,
    search_hiring_posts,
    search_jobs_for_keywords,
    sort_by_match_score,
    write_linkedin_excel,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .xlsx path (default: <linkedin_jobs.output_dir>/linkedin_jobs_<today>.xlsx)",
    )
    args = parser.parse_args()

    api_token = os.environ.get("APIFY_API_TOKEN")
    if not api_token:
        print("APIFY_API_TOKEN is not set. Get a token from your Apify account and run:")
        print("  set APIFY_API_TOKEN=your-token-here   (Windows cmd)")
        print("  $env:APIFY_API_TOKEN = 'your-token-here'   (PowerShell)")
        raise SystemExit(1)

    settings = load_settings()
    jobs_cfg = settings.linkedin_jobs
    posts_cfg = settings.linkedin_posts

    output_path = args.output or (
        jobs_cfg.output_dir / f"linkedin_jobs_{date.today().isoformat()}.xlsx"
    )

    jobs_client = ApifyLinkedInJobSearch(api_token=api_token)
    job_postings = search_jobs_for_keywords(
        jobs_client,
        keywords=jobs_cfg.keywords,
        location=jobs_cfg.location,
        date_posted=jobs_cfg.date_posted,
        total_limit=jobs_cfg.limit,
    )

    resume_profile = load_resume_profile()
    niche_keywords = load_niche_keywords()
    job_postings = attach_match_scores(
        job_postings, resume_profile, niche_keywords, settings.niches.order
    )
    job_postings = sort_by_match_score(job_postings)

    posts_client = ApifyLinkedInPostSearch(api_token=api_token)
    post_queries = build_post_search_queries(jobs_cfg.keywords)
    raw_posts = search_hiring_posts(
        posts_client,
        queries=post_queries,
        posted_limit=posts_cfg.posted_limit,
        total_limit=posts_cfg.limit,
    )
    hiring_posts = filter_relevant_posts(
        raw_posts,
        jobs_cfg.keywords,
        resume_profile.seniority_mismatch_keywords,
        resume_profile.total_experience_years(),
    )

    write_linkedin_excel(job_postings, hiring_posts, output_path)

    jobs_with_contact = sum(1 for p in job_postings if p.poster_name or p.other_contacts)
    strong_matches = sum(1 for p in job_postings if (p.match_score or 0) >= 70)
    print(f"[Jobs] Searched {len(jobs_cfg.keywords)} keyword(s) in {jobs_cfg.location!r}, posted {jobs_cfg.date_posted}")
    print(f"[Jobs] Found {len(job_postings)} listing(s) (capped at {jobs_cfg.limit})")
    print(f"[Jobs] {jobs_with_contact} listing(s) had a poster name or an email in the description")
    print(f"[Jobs] {strong_matches} listing(s) scored 70+ against your resume profile (sorted to the top)")

    print(f"[Posts] Fetched {len(raw_posts)} raw post(s) (capped at {posts_cfg.limit}), {len(hiring_posts)} passed the relevance filter")

    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
