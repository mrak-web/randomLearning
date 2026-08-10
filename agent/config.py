"""Config loading for the cold-email job agent. See PROJECT.md for the design."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_STORY_BANK_PATH = PROJECT_ROOT / "config" / "story_bank.yaml"


class ConfigError(Exception):
    """Raised when settings.yaml or story_bank.yaml is missing or malformed."""


def _require(d: dict, key: str, context: str) -> Any:
    if key not in d:
        raise ConfigError(f"missing required key '{key}' in {context}")
    return d[key]


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


@dataclass(frozen=True)
class ContactDiscoveryConfig:
    role_search_order: list[str]
    finder_api: str
    min_verification_confidence: float


@dataclass(frozen=True)
class NicheConfig:
    order: list[str]
    active_for_discovery: list[str]

    def is_active_for_discovery(self, niche: str) -> bool:
        return niche in self.active_for_discovery


@dataclass(frozen=True)
class SendConfig:
    start_daily_cap: int
    ramp_step: int
    ramp_ceiling: int
    ramp_interval_days: int
    bounce_rate_circuit_breaker: float


@dataclass(frozen=True)
class FollowupConfig:
    business_days_wait: int
    max_followups: int


@dataclass(frozen=True)
class Settings:
    resume_pdf_path: Path
    attach_resume_by_default: bool
    sender_email: str
    sender_display_name: str
    sourcing_sources: list[str]
    contact_discovery: ContactDiscoveryConfig
    niches: NicheConfig
    send: SendConfig
    followup: FollowupConfig
    db_path: Path
    story_bank_path: Path
    templates_dir: Path


def load_settings(path: Path | None = None) -> Settings:
    path = path or DEFAULT_SETTINGS_PATH
    if not path.exists():
        raise ConfigError(f"settings file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a mapping")

    resume = _require(raw, "resume", str(path))
    sender = _require(raw, "sender", str(path))
    sourcing = _require(raw, "sourcing", str(path))
    contact_discovery = _require(raw, "contact_discovery", str(path))
    niches = _require(raw, "niches", str(path))
    send = _require(raw, "send", str(path))
    followup = _require(raw, "followup", str(path))
    paths = _require(raw, "paths", str(path))

    niche_order = _require(niches, "order", f"{path} niches")
    active_for_discovery = niches.get("active_for_discovery", list(niche_order))
    unknown_active = set(active_for_discovery) - set(niche_order)
    if unknown_active:
        raise ConfigError(
            "niches.active_for_discovery contains niches not listed in niches.order: "
            f"{sorted(unknown_active)}"
        )

    return Settings(
        resume_pdf_path=_resolve_path(_require(resume, "pdf_path", f"{path} resume")),
        attach_resume_by_default=bool(resume.get("attach_by_default", True)),
        sender_email=sender.get("email", ""),
        sender_display_name=_require(sender, "display_name", f"{path} sender"),
        sourcing_sources=list(_require(sourcing, "sources", f"{path} sourcing")),
        contact_discovery=ContactDiscoveryConfig(
            role_search_order=list(
                _require(contact_discovery, "role_search_order", f"{path} contact_discovery")
            ),
            finder_api=_require(contact_discovery, "finder_api", f"{path} contact_discovery"),
            min_verification_confidence=float(
                _require(
                    contact_discovery, "min_verification_confidence", f"{path} contact_discovery"
                )
            ),
        ),
        niches=NicheConfig(order=list(niche_order), active_for_discovery=list(active_for_discovery)),
        send=SendConfig(
            start_daily_cap=int(_require(send, "start_daily_cap", f"{path} send")),
            ramp_step=int(_require(send, "ramp_step", f"{path} send")),
            ramp_ceiling=int(_require(send, "ramp_ceiling", f"{path} send")),
            ramp_interval_days=int(_require(send, "ramp_interval_days", f"{path} send")),
            bounce_rate_circuit_breaker=float(
                _require(send, "bounce_rate_circuit_breaker", f"{path} send")
            ),
        ),
        followup=FollowupConfig(
            business_days_wait=int(_require(followup, "business_days_wait", f"{path} followup")),
            max_followups=int(_require(followup, "max_followups", f"{path} followup")),
        ),
        db_path=_resolve_path(_require(paths, "db_path", f"{path} paths")),
        story_bank_path=_resolve_path(_require(paths, "story_bank_path", f"{path} paths")),
        templates_dir=_resolve_path(_require(paths, "templates_dir", f"{path} paths")),
    )


@dataclass(frozen=True)
class NicheStory:
    key: str
    label: str
    company_context: str
    bullets: list[str]


def load_story_bank(path: Path | None = None) -> dict[str, NicheStory]:
    path = path or DEFAULT_STORY_BANK_PATH
    if not path.exists():
        raise ConfigError(f"story bank file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a mapping")

    story_bank: dict[str, NicheStory] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}[{key}] must be a mapping")
        bullets = _require(entry, "bullets", f"{path}[{key}]")
        if not bullets:
            raise ConfigError(f"{path}[{key}].bullets is empty")
        story_bank[key] = NicheStory(
            key=key,
            label=_require(entry, "label", f"{path}[{key}]"),
            company_context=_require(entry, "company_context", f"{path}[{key}]"),
            bullets=list(bullets),
        )
    return story_bank


def missing_templates(settings: Settings, story_bank: dict[str, NicheStory]) -> list[str]:
    """Niches with a story-bank entry but no template file yet — diagnostic, not fatal."""
    return sorted(
        niche for niche in story_bank if not (settings.templates_dir / f"{niche}.txt").exists()
    )
