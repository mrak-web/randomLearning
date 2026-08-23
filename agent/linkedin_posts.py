"""LinkedIn hiring-post lookup — a companion to agent/linkedin_jobs.py, also a
standalone tool, not wired into the cold-email pipeline. See PROJECT.md §8.1.

Searches LinkedIn's public, logged-out post search (harvestapi/linkedin-post-search
on Apify) for posts where someone is personally discussing a hiring need — e.g. a PM
or HR person posting "we're hiring a Product Manager...", which LinkedIn's formal
Jobs tab (agent/linkedin_jobs.py) never surfaces.

This data is unstructured and much noisier than a Jobs listing: manual sampling
during design turned up recruiter-agency blasts, job-aggregator bot accounts, and
candidates-seeking-work posts mixed in with genuine hiring posts, alongside real
hits (e.g. an actual PM posting a referral link for their own team's open role).

Two client-side filters cut that noise (applied in filter_relevant_posts, not via
the Actor's own `authorKeywords` input — that parameter zeroed out real results in
manual testing and its exact matching semantics aren't documented, so filtering
ourselves is more verifiable):
  - the post's content must mention a hiring-intent phrase AND one of the configured
    job-title keywords (reusing linkedin_jobs' keyword list) — a loose
    search-relevance match from LinkedIn isn't enough on its own
  - the author's headline must match the same HR/Product role-signal keywords
    already used for contact discovery (agent/contact_discovery.ROLE_KEYWORDS) —
    this alone dropped two bot/aggregator accounts (generic "N followers" author
    info, no real headline) in manual testing
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from openpyxl import Workbook

from agent.contact_discovery import ROLE_KEYWORDS
from agent.linkedin_jobs import autosize_columns

APIFY_RUN_SYNC_URL_TEMPLATE = "https://api.apify.com/v2/acts/{actor_slug}/run-sync-get-dataset-items"

HIRING_INTENT_KEYWORDS = (
    "hiring",
    "we're hiring",
    "we are hiring",
    "looking for a",
    "looking to hire",
    "open position",
    "job opening",
    "job opportunity",
    "referral",
    "join our team",
    "join us as",
    "vacancy",
)

EXCERPT_MAX_CHARS = 600

# See looks_like_hiring_post's docstring for why matching is windowed to the opening
# of the post rather than the full text.
HEADLINE_WINDOW_CHARS = 300

EXCEL_HEADERS = [
    "Contact Name",
    "Contact Headline",
    "Contact LinkedIn Profile",
    "Company (if detected)",
    "Post Excerpt",
    "Post Link",
    "Posted At",
]


class LinkedInPostSearchError(Exception):
    """Raised when the Apify Actor call fails or returns something unusable."""


@dataclass(frozen=True)
class HiringPost:
    post_id: str | None
    author_name: str | None
    author_headline: str | None
    author_profile_url: str | None
    content: str | None
    link: str | None
    posted_at: str | None
    company_name: str | None


def looks_like_hiring_post(content: str | None, title_keywords: Iterable[str]) -> bool:
    """True if the post's *opening* announces a hiring need for one of the titles.

    Both checks are restricted to the first HEADLINE_WINDOW_CHARS of the post, not
    the full text. Manual sampling during design showed the real signal — "UKG is
    hiring a Senior Product Manager...", "We're Hiring! | Product Lead..." — always
    sits in the opening line, while false positives came from the title keyword
    appearing incidentally deep in a body ("...you'll work with backend engineers
    and product managers") for a post actually hiring an unrelated role. Windowing
    the match to the opening cut that noise out in manual testing.
    """
    if not content:
        return False
    headline = content[:HEADLINE_WINDOW_CHARS].lower()
    has_intent = any(keyword in headline for keyword in HIRING_INTENT_KEYWORDS)
    has_title = any(keyword.lower() in headline for keyword in title_keywords)
    return has_intent and has_title


def author_matches_role_signal(headline: str | None) -> bool:
    if not headline:
        return False
    lowered = headline.lower()
    return any(
        keyword in lowered for keywords in ROLE_KEYWORDS.values() for keyword in keywords
    )


def filter_relevant_posts(
    posts: Iterable[HiringPost], title_keywords: Iterable[str]
) -> list[HiringPost]:
    """Keeps posts that plausibly ARE a hiring announcement from a hiring-relevant
    person, dropping recruiter-agency blasts for unrelated roles, bot/aggregator
    accounts, and candidates-seeking-work posts that a loose LinkedIn search-relevance
    match still lets through. Not perfect precision by design — this is a keyword
    heuristic, same discipline as the rule-based company classifier (no NLP/LLM
    extraction) — false positives are expected and fine for a human to skim past.
    """
    title_keywords = list(title_keywords)
    return [
        post
        for post in posts
        if looks_like_hiring_post(post.content, title_keywords)
        and author_matches_role_signal(post.author_headline)
    ]


def _excerpt(content: str | None, max_chars: int = EXCERPT_MAX_CHARS) -> str | None:
    if not content:
        return None
    stripped = content.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars].rstrip() + "…"


def _extract_company_name(raw_attributes) -> str | None:
    if not isinstance(raw_attributes, list):
        return None
    for attr in raw_attributes:
        if isinstance(attr, dict):
            company = attr.get("company")
            if isinstance(company, dict) and company.get("name"):
                return company["name"]
    return None


def _to_post(item: dict) -> HiringPost:
    author = item.get("author") or {}
    posted_at = item.get("postedAt") or {}
    return HiringPost(
        post_id=item.get("id"),
        author_name=author.get("name"),
        author_headline=author.get("info"),
        author_profile_url=author.get("linkedinUrl"),
        content=item.get("content"),
        link=item.get("linkedinUrl"),
        posted_at=posted_at.get("date"),
        company_name=_extract_company_name(item.get("contentAttributes")),
    )


class LinkedInPostSearchClient(ABC):
    """A pluggable post-search source. Apify's harvestapi Actor is the only one today."""

    @abstractmethod
    def search(self, query: str, posted_limit: str, limit: int) -> list[HiringPost]:
        """Runs one search for a single free-text query, capped at `limit` raw results."""


class ApifyLinkedInPostSearch(LinkedInPostSearchClient):
    """Calls harvestapi/linkedin-post-search via Apify's run-sync REST endpoint.

    Same synchronous-call pattern as ApifyLinkedInJobSearch (agent/linkedin_jobs.py) —
    small result sets, one blocking HTTP call per query.
    """

    actor_slug = "harvestapi~linkedin-post-search"

    def __init__(
        self,
        api_token: str,
        session: requests.Session | None = None,
        timeout: float = 280.0,
    ):
        if not api_token:
            raise LinkedInPostSearchError("Apify API token is required")
        self.api_token = api_token
        self.session = session or requests.Session()
        self.timeout = timeout

    def search(self, query: str, posted_limit: str, limit: int) -> list[HiringPost]:
        if limit <= 0:
            return []

        url = APIFY_RUN_SYNC_URL_TEMPLATE.format(actor_slug=self.actor_slug)
        payload = {
            "searchQueries": [query],
            "postedLimit": posted_limit,
            "sortBy": "date",
            "maxPosts": limit,
            "profileScraperMode": "short",
        }
        try:
            response = self.session.post(
                url, params={"token": self.api_token}, json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise LinkedInPostSearchError(
                f"Apify request failed for query={query!r}: {exc}"
            ) from exc

        # See ApifyLinkedInJobSearch.search for why 201 (not just 200) is success.
        if not (200 <= response.status_code < 300):
            raise LinkedInPostSearchError(
                f"Apify Actor call failed for query={query!r}: HTTP {response.status_code}"
            )

        try:
            items = response.json()
        except ValueError as exc:
            raise LinkedInPostSearchError(
                f"Apify returned a non-JSON response for query={query!r}"
            ) from exc

        if not isinstance(items, list):
            raise LinkedInPostSearchError(f"Unexpected Apify response shape for query={query!r}")

        return [_to_post(item) for item in items]


def build_post_search_queries(title_keywords: Iterable[str], location: str) -> list[str]:
    """One quoted-phrase query per job title, biased toward hiring intent + location."""
    return [f'"{title}" hiring {location}' for title in title_keywords]


def search_hiring_posts(
    client: LinkedInPostSearchClient,
    queries: Iterable[str],
    posted_limit: str,
    total_limit: int,
) -> list[HiringPost]:
    """Runs one search per query, deduped by post link, capped at total_limit RAW
    fetches overall — this cap drives Apify spend (proportional to total_limit,
    regardless of how many queries there are); the relevance filter above runs
    afterward and will typically leave fewer rows than total_limit.
    """
    collected: list[HiringPost] = []
    seen_keys: set[str] = set()

    for query in queries:
        remaining = total_limit - len(collected)
        if remaining <= 0:
            break

        for post in client.search(query, posted_limit, remaining):
            key = post.link or post.post_id
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            collected.append(post)
            if len(collected) >= total_limit:
                break

    return collected


def populate_hiring_posts_sheet(sheet, posts: list[HiringPost]) -> None:
    """Fills an already-created worksheet with the hiring-post columns and autosizes it.

    Factored out so agent/linkedin_export.py can put this sheet alongside the
    "LinkedIn Jobs" sheet in one combined workbook.
    """
    sheet.append(EXCEL_HEADERS)

    for post in posts:
        row = [
            post.author_name,
            post.author_headline,
            post.author_profile_url,
            post.company_name,
            _excerpt(post.content),
            post.link,
            post.posted_at,
        ]
        sheet.append(row)
        written_row = sheet.max_row
        if post.author_profile_url:
            sheet.cell(row=written_row, column=3).hyperlink = post.author_profile_url
        if post.link:
            sheet.cell(row=written_row, column=6).hyperlink = post.link

    autosize_columns(sheet)


def write_hiring_posts_excel(posts: list[HiringPost], output_path: Path) -> None:
    """Writes hiring posts to a single-sheet .xlsx (standalone use / testing)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Hiring Posts"
    populate_hiring_posts_sheet(sheet, posts)

    workbook.save(output_path)
