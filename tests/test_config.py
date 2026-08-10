from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import (
    ConfigError,
    load_settings,
    load_story_bank,
    missing_templates,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_load_settings_against_real_config():
    settings = load_settings()

    assert settings.niches.order == ["consumer", "fintech", "data_saas", "ai_devtools"]
    assert settings.db_path == (REPO_ROOT / "agent.db").resolve()
    assert settings.templates_dir == (REPO_ROOT / "config" / "templates").resolve()
    assert settings.contact_discovery.finder_api == "hunter"


def test_active_for_discovery_is_consumer_only():
    settings = load_settings()

    assert settings.niches.active_for_discovery == ["consumer"]
    assert settings.niches.is_active_for_discovery("consumer") is True
    assert settings.niches.is_active_for_discovery("fintech") is False
    assert settings.niches.is_active_for_discovery("data_saas") is False
    assert settings.niches.is_active_for_discovery("ai_devtools") is False


def test_load_settings_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_settings(REPO_ROOT / "config" / "does_not_exist.yaml")


def test_load_settings_missing_required_key_raises(tmp_path: Path):
    broken = tmp_path / "settings.yaml"
    broken.write_text("resume:\n  pdf_path: './data/resume.pdf'\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="sender"):
        load_settings(broken)


def test_active_for_discovery_rejects_unknown_niche(tmp_path: Path):
    broken = tmp_path / "settings.yaml"
    broken.write_text(
        """
resume:
  pdf_path: "./data/resume.pdf"
  attach_by_default: true
sender:
  email: ""
  display_name: "Test"
gmail:
  credentials_path: "./data/gmail_credentials.json"
  token_path: "./data/gmail_token.json"
sourcing:
  sources: [manual_csv]
contact_discovery:
  role_search_order: [hr]
  finder_api: hunter
  min_verification_confidence: 0.7
niches:
  order: [consumer, fintech]
  active_for_discovery: [consumer, made_up_niche]
send:
  start_daily_cap: 10
  ramp_step: 2
  ramp_ceiling: 25
  ramp_interval_days: 7
  bounce_rate_circuit_breaker: 0.05
  min_delay_seconds: 30
  max_delay_seconds: 120
followup:
  business_days_wait: 5
  max_followups: 1
paths:
  db_path: "./agent.db"
  story_bank_path: "./config/story_bank.yaml"
  templates_dir: "./config/templates"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="made_up_niche"):
        load_settings(broken)


def test_load_story_bank_against_real_config():
    story_bank = load_story_bank()

    assert set(story_bank) == {"consumer", "fintech", "data_saas", "ai_devtools"}
    for niche, story in story_bank.items():
        assert story.key == niche
        assert story.label
        assert story.company_context
        assert len(story.bullets) >= 1


def test_load_story_bank_missing_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        load_story_bank(REPO_ROOT / "config" / "does_not_exist.yaml")


def test_load_story_bank_empty_bullets_raises(tmp_path: Path):
    broken = tmp_path / "story_bank.yaml"
    broken.write_text(
        "consumer:\n  label: 'Consumer'\n  company_context: 'x'\n  bullets: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="bullets is empty"):
        load_story_bank(broken)


def test_missing_templates_reflects_current_repo_state():
    settings = load_settings()
    story_bank = load_story_bank()

    missing = missing_templates(settings, story_bank)

    # All four niche templates exist as of module 5 (email generation).
    assert missing == []
