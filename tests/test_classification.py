from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agent.classification import (
    build_classification_text,
    classify_pending_companies,
    classify_text,
    score_niches,
)
from agent.config import ConfigError, load_niche_keywords, load_settings
from agent.db import connect, init_db
from agent.sourcing import ManualCsvSource, import_companies

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


@pytest.fixture
def niche_keywords():
    return load_niche_keywords()


NICHE_ORDER = ["consumer", "fintech", "data_saas", "ai_devtools"]


def test_load_niche_keywords_against_real_config(niche_keywords):
    assert set(niche_keywords) == {"consumer", "fintech", "data_saas", "ai_devtools"}
    for niche, keywords in niche_keywords.items():
        assert len(keywords) > 0


def test_load_niche_keywords_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_niche_keywords(REPO_ROOT / "config" / "does_not_exist.yaml")


def test_load_niche_keywords_empty_list_raises(tmp_path: Path):
    broken = tmp_path / "niche_keywords.yaml"
    broken.write_text("consumer: []\nfintech:\n  - fintech\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="no keywords"):
        load_niche_keywords(broken)


def test_score_niches_counts_whole_word_matches(niche_keywords):
    scores = score_niches("Meesho marketplace consumer app", niche_keywords)

    assert scores["consumer"] >= 2  # "marketplace" and "consumer"
    assert scores["fintech"] == 0


def test_score_niches_does_not_false_positive_on_substrings(niche_keywords):
    # "email" contains "ai" and "html" contains "ml" as substrings, but neither
    # is a whole-word match, so ai_devtools should not score on this text.
    scores = score_niches("email html support desk for retail stores", niche_keywords)

    assert scores["ai_devtools"] == 0
    assert scores["consumer"] >= 1  # "retail"


def test_classify_text_picks_highest_scoring_niche(niche_keywords):
    niche = classify_text("Zomato food delivery grocery app", niche_keywords, NICHE_ORDER)
    assert niche == "consumer"

    niche = classify_text("Razorpay payments lending platform", niche_keywords, NICHE_ORDER)
    assert niche == "fintech"


def test_classify_text_ties_break_toward_consumer(niche_keywords):
    # "marketplace" (consumer) and "lending" (fintech) each score exactly 1.
    niche = classify_text("marketplace lending", niche_keywords, NICHE_ORDER)
    assert niche == "consumer"


def test_classify_text_ties_break_by_order_when_consumer_not_tied(niche_keywords):
    # "lending" (fintech) and "saas" (data_saas) tie at 1 each; consumer scores 0.
    niche = classify_text("lending saas", niche_keywords, NICHE_ORDER)
    assert niche == "fintech"


def test_classify_text_returns_none_when_unclassifiable(niche_keywords):
    niche = classify_text("Generic Holdings Pvt Ltd", niche_keywords, NICHE_ORDER)
    assert niche is None


def test_build_classification_text_joins_name_and_tags():
    text = build_classification_text("Acme", ["fintech", "payments"])
    assert text == "Acme fintech payments"


def test_classify_pending_companies_updates_db(settings, niche_keywords):
    init_db(settings.db_path, settings)
    csv_path = REPO_ROOT / "tests" / "fixtures" / "companies_sample.csv"

    with connect(settings.db_path) as conn:
        import_companies(conn, ManualCsvSource(csv_path).fetch())
        stats = classify_pending_companies(conn, niche_keywords, NICHE_ORDER)

        rows = conn.execute("SELECT name, niche, status FROM companies").fetchall()

    # All 5 fixture companies (post-dedup) are consumer-tagged.
    assert stats.classified == 5
    assert stats.unclassifiable == 0
    assert stats.by_niche["consumer"] == 5
    for row in rows:
        assert row["niche"] == "consumer"
        assert row["status"] == "classified"


def test_classify_pending_companies_flags_unclassifiable_as_skipped(settings, niche_keywords):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO companies (name, domain, source) VALUES (?, ?, ?)",
            ("Generic Holdings Pvt Ltd", "genericholdings.example", "manual_csv"),
        )
        conn.commit()

        stats = classify_pending_companies(conn, niche_keywords, NICHE_ORDER)
        row = conn.execute(
            "SELECT niche, status FROM companies WHERE domain = 'genericholdings.example'"
        ).fetchone()

    assert stats.classified == 0
    assert stats.unclassifiable == 1
    assert row["niche"] is None
    assert row["status"] == "skipped"


def test_classify_pending_companies_ignores_already_processed_rows(settings, niche_keywords):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO companies (name, domain, source, niche, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Already Classified Co", "already.example", "manual_csv", "fintech", "classified"),
        )
        conn.commit()

        stats = classify_pending_companies(conn, niche_keywords, NICHE_ORDER)
        row = conn.execute(
            "SELECT niche, status FROM companies WHERE domain = 'already.example'"
        ).fetchone()

    assert stats.classified == 0
    assert stats.unclassifiable == 0
    # Untouched — niche/status weren't reset even though "fintech" isn't in the name/tags.
    assert row["niche"] == "fintech"
    assert row["status"] == "classified"
