"""Rule-based resume-to-job match scoring for the LinkedIn Job Lookup tool. See
PROJECT.md's LinkedIn Job Lookup section.

Deliberately NOT an LLM call. The resume barely changes, so it's hand-curated once
into config/resume_profile.yaml (same discipline as story_bank.yaml, PROJECT.md §3)
and every job is scored against that fixed profile with plain keyword/regex logic —
zero per-run API cost, no ANTHROPIC_API_KEY needed. This is a deliberate pivot away
from an earlier LLM-per-job design, after Arjun pushed back on paying an API cost to
re-read an unchanging resume on every run (2026-08-23).

Not perfect precision by design — a keyword/regex heuristic, not semantic
understanding — same discipline as the rule-based niche classifier (agent/
classification.py) this module reuses for the domain-fit dimension.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from agent.classification import classify_text
from agent.config import ResumeProfile
from agent.linkedin_jobs import LinkedInJobPosting

# Matches "3+ years", "3-5 years", "minimum 3 years of experience", etc. Takes the
# smallest number found across the whole text as the effective minimum-years bar —
# a heuristic that, if wrong, errs toward NOT penalizing Arjun unfairly (a JD
# mentioning a smaller unrelated number elsewhere would only lower the bar, not
# raise it).
YEARS_PATTERN = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?\+?\s*years?", re.IGNORECASE)

SCORE_WEIGHTS = {
    "seniority": 0.35,
    "experience": 0.30,
    "skills": 0.20,
    "domain": 0.15,
}


@dataclass(frozen=True)
class JobMatchScore:
    score: int  # 0-100, higher = better predicted fit / callback odds
    reasons: tuple[str, ...] = ()


def extract_min_years_required(text: str | None) -> int | None:
    if not text:
        return None
    matches = [int(m) for m in YEARS_PATTERN.findall(text)]
    return min(matches) if matches else None


def score_seniority(
    title: str | None, description: str | None, profile: ResumeProfile
) -> tuple[int, str | None]:
    text = f"{title or ''} {description or ''}".lower()
    for keyword in profile.seniority_mismatch_keywords:
        if keyword.lower() in text:
            return 10, f"role signals '{keyword.strip()}' — above current level"
    return 100, None


def score_experience(
    description: str | None, profile: ResumeProfile
) -> tuple[int, str | None]:
    required = extract_min_years_required(description)
    if required is None:
        return 70, None  # no explicit bar stated — neutral, don't penalize a guess
    have = profile.total_experience_years()
    if have >= required:
        return 100, f"meets stated {required}+ yr requirement ({have:.1f} yrs)"
    gap = required - have
    if gap <= 1:
        return 60, f"~{gap:.1f} yr short of stated {required}+ yr requirement"
    if gap <= 3:
        return 25, f"{gap:.1f} yr short of stated {required}+ yr requirement"
    return 5, f"{gap:.1f} yr short of stated {required}+ yr requirement — large gap"


def score_skills(
    description: str | None, profile: ResumeProfile
) -> tuple[int, str | None]:
    if not description:
        return 50, None
    lowered = description.lower()
    pool = profile.core_skills + profile.core_strengths
    matched = [s for s in pool if s.lower() in lowered]
    if not matched:
        return 30, None
    ratio = len(matched) / len(pool)
    score = min(100, round(40 + ratio * 300))
    return score, f"matched: {', '.join(matched[:5])}"


def score_domain_fit(
    title: str | None,
    description: str | None,
    profile: ResumeProfile,
    niche_keywords: dict[str, list[str]],
    niche_order: list[str],
) -> tuple[int, str | None]:
    text = " ".join(filter(None, [title, description]))
    niche = classify_text(text, niche_keywords, niche_order)
    if niche is None or niche not in profile.domain_fit_order:
        return 50, None
    rank = profile.domain_fit_order.index(niche)
    score = max(55, 100 - rank * 15)
    return score, f"domain: {niche}"


def compute_match_score(
    posting: LinkedInJobPosting,
    profile: ResumeProfile,
    niche_keywords: dict[str, list[str]],
    niche_order: list[str],
) -> JobMatchScore:
    seniority_score, seniority_note = score_seniority(
        posting.title, posting.description_text, profile
    )
    experience_score, experience_note = score_experience(posting.description_text, profile)
    skills_score, skills_note = score_skills(posting.description_text, profile)
    domain_score, domain_note = score_domain_fit(
        posting.title, posting.description_text, profile, niche_keywords, niche_order
    )

    total = (
        seniority_score * SCORE_WEIGHTS["seniority"]
        + experience_score * SCORE_WEIGHTS["experience"]
        + skills_score * SCORE_WEIGHTS["skills"]
        + domain_score * SCORE_WEIGHTS["domain"]
    )
    reasons = tuple(
        note for note in (seniority_note, experience_note, skills_note, domain_note) if note
    )
    return JobMatchScore(score=round(total), reasons=reasons)


def attach_match_scores(
    postings: list[LinkedInJobPosting],
    profile: ResumeProfile,
    niche_keywords: dict[str, list[str]],
    niche_order: list[str],
) -> list[LinkedInJobPosting]:
    """Returns new postings with match_score/match_reasons filled in.

    LinkedInJobPosting is frozen, so this rebuilds each record via
    dataclasses.replace rather than mutating in place.
    """
    result = []
    for posting in postings:
        match = compute_match_score(posting, profile, niche_keywords, niche_order)
        result.append(
            dataclasses.replace(posting, match_score=match.score, match_reasons=match.reasons)
        )
    return result


def sort_by_match_score(postings: list[LinkedInJobPosting]) -> list[LinkedInJobPosting]:
    """Descending by match_score; postings with no score sort last."""
    return sorted(
        postings, key=lambda p: p.match_score if p.match_score is not None else -1, reverse=True
    )
