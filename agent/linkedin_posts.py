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

Four client-side filters cut that noise (applied in filter_relevant_posts, not via
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
  - the post's text must not contain a seniority-mismatch keyword ("senior",
    "principal", …) from the resume profile — mirrors the Jobs sheet's seniority
    dimension (agent.resume_match.score_seniority)
  - the post's text must not state a minimum-years bar Arjun falls clearly short of
    (mirrors agent.resume_match.score_experience/extract_min_years_required, reused
    directly rather than re-implemented) — added after a real run let an "8-12
    Years" listing through: it named no seniority *word*, only a numeric bar, which
    the keyword check alone can't catch
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
from agent.resume_match import extract_min_years_required

APIFY_RUN_SYNC_URL_TEMPLATE = "https://api.apify.com/v2/acts/{actor_slug}/run-sync-get-dataset-items"

HIRING_INTENT_KEYWORDS = (
    "hiring",
    "we're hiring",
    "we are hiring",
    "looking for a",
    "looking to hire",
    "open position",
    "open role",
    "roles open",
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


def has_seniority_mismatch(
    content: str | None, seniority_mismatch_keywords: Iterable[str]
) -> bool:
    """True if the post's full text contains a seniority-title word above Arjun's
    current target ("Senior", "Principal", …) — reuses the same keyword list already
    applied to the Jobs sheet (agent.resume_match.score_seniority / config/
    resume_profile.yaml) so both sheets agree on what counts as a mismatch. Checked
    against the full post text, not just the opening window looks_like_hiring_post
    uses, since this kind of language can appear anywhere in the post.

    This only catches seniority *words* — a post stating a plain numeric years bar
    ("Experience: 8-12 Years") with no seniority word in it slips past this check;
    see has_experience_gap below for that case (a real one found in manual testing:
    "Product Manager – Enterprise Wi-Fi... Experience: 8–12 Years").
    """
    if not content:
        return False
    lowered = content.lower()
    return any(keyword.lower() in lowered for keyword in seniority_mismatch_keywords)


def has_experience_gap(
    content: str | None,
    total_experience_years: float,
    max_gap_years: float = 3.0,
) -> bool:
    """True if the post states a minimum-years bar Arjun falls short of by more than
    max_gap_years. Reuses agent.resume_match.extract_min_years_required directly
    rather than re-implementing the regex. This is a pass/fail cut, not the Jobs
    sheet's graded score — Hiring Posts already returns few results, so the default
    only drops clearly out-of-reach roles (a gap of 3+ years, e.g. "8-12 Years" vs.
    ~2 years of experience) rather than borderline ones (a gap of 1 year) a human
    might still want to see and judge for themselves.
    """
    required = extract_min_years_required(content)
    if required is None:
        return False
    return (required - total_experience_years) > max_gap_years


def filter_relevant_posts(
    posts: Iterable[HiringPost],
    title_keywords: Iterable[str],
    seniority_mismatch_keywords: Iterable[str] = (),
    total_experience_years: float | None = None,
) -> list[HiringPost]:
    """Keeps posts that plausibly ARE a hiring announcement from a hiring-relevant
    person for a role at Arjun's level, dropping recruiter-agency blasts for
    unrelated roles, bot/aggregator accounts, candidates-seeking-work posts that a
    loose LinkedIn search-relevance match still lets through, and senior-level
    listings he doesn't qualify for yet (by seniority word OR by a stated years bar
    he falls clearly short of — see has_seniority_mismatch vs. has_experience_gap
    above for why both checks exist). Not perfect precision by design — this is a
    keyword/regex heuristic, same discipline as the rule-based company classifier
    (no NLP/LLM extraction) — false positives are expected and fine for a human to
    skim past. seniority_mismatch_keywords defaults to empty and
    total_experience_years defaults to None (both checks off) so existing callers
    that don't pass them keep their prior behavior.
    """
    title_keywords = list(title_keywords)
    seniority_mismatch_keywords = list(seniority_mismatch_keywords)
    return [
        post
        for post in posts
        if looks_like_hiring_post(post.content, title_keywords)
        and author_matches_role_signal(post.author_headline)
        and not has_seniority_mismatch(post.content, seniority_mismatch_keywords)
        and not (
            total_experience_years is not None
            and has_experience_gap(post.content, total_experience_years)
        )
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


# LinkedIn's post search has no true geo filter (unlike the Jobs actor), so these
# widen query coverage beyond the literal word "India". Real posts Arjun shared from
# his own feed (2026-08-23) named a specific city ("Bangalore") or no location at
# all rather than the country name -- a country-only query was silently cutting
# recall, since these are quoted-phrase text queries, not filters.
DEFAULT_QUERY_LOCATIONS: tuple[str, ...] = (
    "India",
    "Bengaluru",
    "Bangalore",
    "Mumbai",
    "Delhi",
    "Gurgaon",
    "Hyderabad",
    "Pune",
    "Noida",
)


def build_post_search_queries(
    title_keywords: Iterable[str], locations: Iterable[str] | None = None
) -> list[str]:
    """One quoted-phrase query per title, times a location-free variant plus each
    of `locations` (DEFAULT_QUERY_LOCATIONS if not given).

    Ordered location-slot-major (every title at one location before moving to the
    next) so that if the raw-fetch budget runs out partway through, it has already
    sampled all titles broadly rather than exhausting the budget drilling into one
    title's city variants before ever trying the others.
    """
    title_keywords = list(title_keywords)
    location_slots: list[str | None] = [None, *(locations if locations is not None else DEFAULT_QUERY_LOCATIONS)]

    queries = []
    for location in location_slots:
        for title in title_keywords:
            query = f'"{title}" hiring' if location is None else f'"{title}" hiring {location}'
            queries.append(query)
    return queries


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
