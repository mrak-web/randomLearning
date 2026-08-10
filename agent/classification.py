"""Rule-based niche classification for the cold-email job agent. See PROJECT.md §4.2.

Runs immediately after sourcing and before contact discovery — classification is free
(no external API), and niches.active_for_discovery (settings.yaml) can only gate
contact discovery once a company's niche is actually known.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    pattern = _PATTERN_CACHE.get(keyword)
    if pattern is None:
        pattern = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
        _PATTERN_CACHE[keyword] = pattern
    return pattern


def build_classification_text(name: str, raw_tags: list[str]) -> str:
    return " ".join([name, *raw_tags])


def score_niches(text: str, niche_keywords: dict[str, list[str]]) -> dict[str, int]:
    scores = {niche: 0 for niche in niche_keywords}
    for niche, keywords in niche_keywords.items():
        for keyword in keywords:
            if _keyword_pattern(keyword).search(text):
                scores[niche] += 1
    return scores


def classify_text(
    text: str, niche_keywords: dict[str, list[str]], tie_break_order: list[str]
) -> str | None:
    """Highest-scoring niche wins; ties broken by position in tie_break_order.

    Returns None if nothing scored above zero — the caller should treat that as
    unclassifiable rather than guessing.
    """
    scores = score_niches(text, niche_keywords)
    max_score = max(scores.values(), default=0)
    if max_score == 0:
        return None

    top = {niche for niche, score in scores.items() if score == max_score}
    for niche in tie_break_order:
        if niche in top:
            return niche
    # Shouldn't happen if tie_break_order covers every niche in niche_keywords,
    # but fall back to a deterministic choice rather than raising.
    return sorted(top)[0]


@dataclass(frozen=True)
class ClassificationStats:
    classified: int
    unclassifiable: int
    by_niche: dict[str, int] = field(default_factory=dict)


def classify_pending_companies(
    conn: sqlite3.Connection,
    niche_keywords: dict[str, list[str]],
    niche_order: list[str],
) -> ClassificationStats:
    """Classifies every company with status='new', leaving already-processed rows alone."""
    rows = conn.execute("SELECT id, name, raw_tags FROM companies WHERE status = 'new'").fetchall()

    classified = 0
    unclassifiable = 0
    by_niche: dict[str, int] = {niche: 0 for niche in niche_order}

    for row in rows:
        raw_tags = json.loads(row["raw_tags"]) if row["raw_tags"] else []
        text = build_classification_text(row["name"], raw_tags)
        niche = classify_text(text, niche_keywords, niche_order)

        if niche is None:
            conn.execute("UPDATE companies SET status = 'skipped' WHERE id = ?", (row["id"],))
            unclassifiable += 1
        else:
            conn.execute(
                "UPDATE companies SET niche = ?, status = 'classified' WHERE id = ?",
                (niche, row["id"]),
            )
            classified += 1
            by_niche[niche] = by_niche.get(niche, 0) + 1

    conn.commit()
    return ClassificationStats(classified=classified, unclassifiable=unclassifiable, by_niche=by_niche)
