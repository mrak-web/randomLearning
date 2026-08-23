from __future__ import annotations

from datetime import date

import pytest

from agent.config import ResumeProfile
from agent.linkedin_jobs import LinkedInJobPosting
from agent.resume_match import (
    attach_match_scores,
    compute_match_score,
    extract_min_years_required,
    score_domain_fit,
    score_experience,
    score_seniority,
    score_skills,
    sort_by_match_score,
)

NICHE_KEYWORDS = {
    "consumer": ["marketplace", "ride-hailing", "rides", "consumer"],
    "fintech": ["payments", "bnpl", "fintech"],
    "data_saas": ["analytics", "b2b saas", "data platform"],
    "ai_devtools": ["ai agent", "developer tools", "automation"],
}
NICHE_ORDER = ["consumer", "fintech", "data_saas", "ai_devtools"]


def profile(**overrides) -> ResumeProfile:
    defaults = dict(
        name="Arjun Khanna",
        full_time_start_date=date(2024, 6, 1),
        target_level="entry_level_pm",
        education="non_technical",
        core_skills=["SQL", "Advanced Excel", "JIRA", "Power BI", "Tableau"],
        core_strengths=["user research", "stakeholder management", "growth metrics"],
        domain_fit_order=["consumer", "data_saas", "fintech", "ai_devtools"],
        seniority_mismatch_keywords=["senior", "principal", "director", "head of product"],
    )
    defaults.update(overrides)
    return ResumeProfile(**defaults)


def posting(title: str, description_text: str | None, **overrides) -> LinkedInJobPosting:
    defaults = dict(
        job_id="job1",
        title=title,
        company_name="Acme",
        link="https://in.linkedin.com/jobs/view/job1",
        location="Bengaluru, India",
        posted_at="2026-08-20",
        applicants_count="50",
        poster_name=None,
        poster_title=None,
        poster_profile_url=None,
        description_text=description_text,
    )
    defaults.update(overrides)
    return LinkedInJobPosting(**defaults)


# ---------------------------------------------------------------------------
# ResumeProfile.total_experience_years
# ---------------------------------------------------------------------------


def test_total_experience_years_computed_dynamically():
    p = profile(full_time_start_date=date(2024, 6, 1))
    years = p.total_experience_years(as_of=date(2026, 8, 23))
    assert 2.1 < years < 2.3


# ---------------------------------------------------------------------------
# extract_min_years_required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We require 5+ years of experience", 5),
        ("3-5 years of relevant experience", 3),
        ("minimum 2 years experience needed", 2),
        ("No experience requirement mentioned here", None),
        (None, None),
    ],
)
def test_extract_min_years_required(text, expected):
    assert extract_min_years_required(text) == expected


def test_extract_min_years_required_takes_the_smallest_match():
    text = "8+ years managing teams, though junior candidates with 1 year may apply"
    assert extract_min_years_required(text) == 1


# ---------------------------------------------------------------------------
# score_seniority
# ---------------------------------------------------------------------------


def test_score_seniority_penalizes_mismatch_keyword():
    p = profile()
    score, note = score_seniority("Senior Product Manager", "some description", p)
    assert score == 10
    assert "senior" in note.lower()


def test_score_seniority_full_score_when_no_mismatch():
    p = profile()
    score, note = score_seniority("Associate Product Manager", "great entry-level role", p)
    assert score == 100
    assert note is None


# ---------------------------------------------------------------------------
# score_experience
# ---------------------------------------------------------------------------


def test_score_experience_no_requirement_stated_is_neutral():
    p = profile(full_time_start_date=date(2024, 6, 1))
    score, note = score_experience("Great team, exciting product", p)
    assert score == 70
    assert note is None


def test_score_experience_meets_requirement():
    p = profile(full_time_start_date=date(2020, 1, 1))  # ~6+ years by test-authoring time
    score, note = score_experience("Requires 2+ years of experience", p)
    assert score == 100
    assert "meets" in note.lower()


def test_score_experience_large_gap_scores_low():
    p = profile(full_time_start_date=date(2024, 6, 1))  # ~2 years
    score, note = score_experience("Requires 8+ years of experience", p)
    assert score == 5
    assert "large gap" in note.lower()


def test_score_experience_small_gap_scores_moderately():
    p = profile(full_time_start_date=date(2024, 6, 1))  # ~2.2 years as of 2026-08
    score, note = score_experience("Requires 3+ years of experience", p)
    assert score in (60, 25)  # depends on exact "today", but never full/near-zero
    assert note is not None


# ---------------------------------------------------------------------------
# score_skills
# ---------------------------------------------------------------------------


def test_score_skills_no_description_is_neutral():
    p = profile()
    score, note = score_skills(None, p)
    assert score == 50
    assert note is None


def test_score_skills_no_matches_scores_low():
    p = profile()
    score, note = score_skills("We need someone great at leadership", p)
    assert score == 30
    assert note is None


def test_score_skills_matches_boost_score():
    p = profile()
    score, note = score_skills("Must know SQL, Power BI, and have strong user research skills", p)
    assert score > 30
    assert "SQL" in note


# ---------------------------------------------------------------------------
# score_domain_fit
# ---------------------------------------------------------------------------


def test_score_domain_fit_best_rank_scores_highest():
    p = profile(domain_fit_order=["consumer", "fintech", "data_saas", "ai_devtools"])
    score, note = score_domain_fit(
        "Product Manager", "join our ride-hailing marketplace team", p, NICHE_KEYWORDS, NICHE_ORDER
    )
    assert score == 100
    assert "consumer" in note


def test_score_domain_fit_lower_rank_scores_lower():
    p = profile(domain_fit_order=["consumer", "fintech", "data_saas", "ai_devtools"])
    score, note = score_domain_fit(
        "Product Manager", "we build ai agent developer tools for automation", p, NICHE_KEYWORDS, NICHE_ORDER
    )
    assert score < 100
    assert "ai_devtools" in note


def test_score_domain_fit_unclassifiable_is_neutral():
    p = profile()
    score, note = score_domain_fit("Product Manager", "generic role with no domain signal", p, NICHE_KEYWORDS, NICHE_ORDER)
    assert score == 50
    assert note is None


# ---------------------------------------------------------------------------
# compute_match_score / attach_match_scores / sort_by_match_score (end-to-end)
# ---------------------------------------------------------------------------


def test_compute_match_score_good_fit_scores_high():
    p = profile(full_time_start_date=date(2024, 6, 1))
    job = posting(
        "Associate Product Manager",
        "1-2 years experience. Join our ride-hailing marketplace team. "
        "SQL and Power BI a plus. Strong user research and stakeholder management skills needed.",
    )
    result = compute_match_score(job, p, NICHE_KEYWORDS, NICHE_ORDER)
    assert result.score >= 70
    assert len(result.reasons) >= 2


def test_compute_match_score_senior_role_scores_low():
    p = profile(full_time_start_date=date(2024, 6, 1))
    job = posting(
        "Senior Product Manager",
        "8+ years of experience required to lead our fintech payments platform.",
    )
    result = compute_match_score(job, p, NICHE_KEYWORDS, NICHE_ORDER)
    assert result.score <= 40


def test_attach_match_scores_fills_in_score_and_reasons():
    p = profile(full_time_start_date=date(2024, 6, 1))
    jobs = [posting("Product Manager", "join our marketplace team")]
    scored = attach_match_scores(jobs, p, NICHE_KEYWORDS, NICHE_ORDER)
    assert scored[0].match_score is not None
    assert isinstance(scored[0].match_reasons, tuple)
    # Original list untouched (frozen dataclass, replace returns new objects).
    assert jobs[0].match_score is None


def test_sort_by_match_score_descending_with_none_last():
    high = posting("job_high", "desc", job_id="high", match_score=90)
    low = posting("job_low", "desc", job_id="low", match_score=10)
    unscored = posting("job_none", "desc", job_id="none", match_score=None)

    result = sort_by_match_score([low, unscored, high])

    assert [p.job_id for p in result] == ["high", "low", "none"]
