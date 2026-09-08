from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agent.config import NicheStory, load_settings, load_story_bank
from agent.db import connect, init_db
from agent.email_generation import (
    RESUME_STORY_NICHE,
    EmailGenerationError,
    build_story_bullets,
    build_story_paragraph,
    generate_pending_emails,
    get_first_name,
    parse_template,
    render_email,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings(tmp_path: Path):
    real_settings = load_settings()
    return dataclasses.replace(real_settings, db_path=tmp_path / "agent.db")


@pytest.fixture
def story_bank():
    return load_story_bank()


# ---------------------------------------------------------------------------
# get_first_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "full_name,expected",
    [
        ("Priya Sharma", "Priya"),
        ("Cher", "Cher"),
        ("  Rahul   Mehta  ", "Rahul"),
        (None, "there"),
        ("", "there"),
        ("   ", "there"),
    ],
)
def test_get_first_name(full_name, expected):
    assert get_first_name(full_name) == expected


# ---------------------------------------------------------------------------
# build_story_paragraph
# ---------------------------------------------------------------------------


def test_build_story_paragraph_stitches_two_bullets_by_default(story_bank):
    paragraph = build_story_paragraph(story_bank["consumer"])

    assert paragraph.startswith("At Rapido (ride-hailing marketplace), I launched")
    assert "I also built" in paragraph
    assert paragraph.count(". ") >= 1
    # Only the first two of the three consumer bullets should appear.
    assert "reverse-bid" not in paragraph


def test_build_story_paragraph_respects_num_bullets(story_bank):
    paragraph = build_story_paragraph(story_bank["consumer"], num_bullets=1)

    assert "I also" not in paragraph
    assert paragraph.startswith("At Rapido")


def test_build_story_paragraph_raises_on_empty_bullets():
    empty_story = NicheStory(key="consumer", label="x", company_context="x", bullets=[])

    with pytest.raises(EmailGenerationError, match="no bullets"):
        build_story_paragraph(empty_story)


# ---------------------------------------------------------------------------
# build_story_bullets
# ---------------------------------------------------------------------------


def test_build_story_bullets_renders_dash_lines_by_default(story_bank):
    bullets = build_story_bullets(story_bank["consumer"])

    lines = bullets.split("\n")
    assert len(lines) == 2
    assert all(line.startswith("- ") for line in lines)
    # Only the first two of the three consumer bullets should appear.
    assert "reverse-bid" not in bullets


def test_build_story_bullets_respects_num_bullets(story_bank):
    bullets = build_story_bullets(story_bank["consumer"], num_bullets=1)

    assert len(bullets.split("\n")) == 1
    assert bullets.startswith("- ")


def test_build_story_bullets_raises_on_empty_bullets():
    empty_story = NicheStory(key="consumer", label="x", company_context="x", bullets=[])

    with pytest.raises(EmailGenerationError, match="no bullets"):
        build_story_bullets(empty_story)


# ---------------------------------------------------------------------------
# parse_template
# ---------------------------------------------------------------------------


def test_parse_template_splits_subject_and_body():
    text = "Subject: Hello {{company_name}}\n\nHi {{contact_first_name}},\n\nBody text.\n"

    subject, body = parse_template(text)

    assert subject == "Hello {{company_name}}"
    assert body == "Hi {{contact_first_name}},\n\nBody text.\n"


def test_parse_template_requires_subject_line():
    with pytest.raises(EmailGenerationError, match="Subject:"):
        parse_template("Hi {{contact_first_name}},\n\nNo subject line here.\n")


def test_parse_template_on_real_templates():
    for niche in ["consumer", "fintech", "data_saas", "ai_devtools"]:
        text = (REPO_ROOT / "config" / "templates" / f"{niche}.txt").read_text(encoding="utf-8")
        subject, body = parse_template(text)
        assert subject
        assert body
        assert "{{company_name}}" in subject or "{{company_name}}" in body


# ---------------------------------------------------------------------------
# render_email
# ---------------------------------------------------------------------------


def test_render_email_substitutes_all_placeholders(story_bank):
    template_text = (REPO_ROOT / "config" / "templates" / "consumer.txt").read_text(
        encoding="utf-8"
    )

    subject, body = render_email(
        template_text,
        company_name="Meesho",
        contact_name="Priya Sharma",
        story=story_bank["consumer"],
        sender_display_name="Arjun Khanna",
    )

    # Company name is back in the subject (2026-08-29) -- personalization + breaks the
    # identical-subject-to-many-recipients spam pattern of the prior fixed subject.
    assert subject == "Seeking Product Roles at Meesho | Currently Product Intern at Rapido | Ashoka University"
    assert "Meesho" in body
    assert "Priya" in body
    assert "Arjun Khanna" in body
    assert "{{" not in subject
    assert "{{" not in body


def test_render_email_includes_sender_phone_in_signature(story_bank):
    template_text = (REPO_ROOT / "config" / "templates" / "consumer.txt").read_text(
        encoding="utf-8"
    )

    _, body = render_email(
        template_text,
        company_name="Meesho",
        contact_name="Priya Sharma",
        story=story_bank["consumer"],
        sender_display_name="Arjun Khanna",
        sender_phone="+91 9466898689",
    )

    assert "+91 9466898689" in body
    assert body.rstrip().endswith("https://www.linkedin.com/in/arjun-khanna-838205213/")


def test_render_email_defaults_sender_phone_to_empty(story_bank):
    template_text = (REPO_ROOT / "config" / "templates" / "consumer.txt").read_text(
        encoding="utf-8"
    )

    _, body = render_email(
        template_text,
        company_name="Meesho",
        contact_name="Priya Sharma",
        story=story_bank["consumer"],
        sender_display_name="Arjun Khanna",
    )

    assert "{{" not in body


def test_render_email_falls_back_to_there_for_missing_contact_name(story_bank):
    template_text = (REPO_ROOT / "config" / "templates" / "consumer.txt").read_text(
        encoding="utf-8"
    )

    _, body = render_email(
        template_text,
        company_name="Meesho",
        contact_name=None,
        story=story_bank["consumer"],
        sender_display_name="Arjun Khanna",
    )

    assert "Hi there," in body


def test_render_email_raises_on_unresolved_placeholder(story_bank):
    template_text = "Subject: {{company_name}} and {{something_unknown}}\n\nBody.\n"

    with pytest.raises(EmailGenerationError, match="unresolved placeholder"):
        render_email(
            template_text,
            company_name="Meesho",
            contact_name="Priya",
            story=story_bank["consumer"],
            sender_display_name="Arjun Khanna",
        )


# ---------------------------------------------------------------------------
# generate_pending_emails (integration against a real temp DB)
# ---------------------------------------------------------------------------


def _insert_company(conn, name, niche, status="classified") -> int:
    cur = conn.execute(
        "INSERT INTO companies (name, source, niche, status) VALUES (?, 'manual_csv', ?, ?)",
        (name, niche, status),
    )
    return cur.lastrowid


def _insert_contact(conn, company_id, name, email, status="verified") -> int:
    cur = conn.execute(
        "INSERT INTO contacts (company_id, name, email, role_category, status) "
        "VALUES (?, ?, ?, 'hr', ?)",
        (company_id, name, email, status),
    )
    return cur.lastrowid


def test_generate_pending_emails_creates_draft(settings, story_bank):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        company_id = _insert_company(conn, "Meesho", "consumer")
        _insert_contact(conn, company_id, "Priya Sharma", "priya@meesho.example")
        conn.commit()

        stats = generate_pending_emails(
            conn, story_bank, settings.templates_dir, "Arjun Khanna", True,
            sender_phone="+91 9466898689",
        )

        row = conn.execute("SELECT * FROM email_queue").fetchone()

    assert stats.generated == 1
    assert row["niche"] == "consumer"
    assert row["kind"] == "initial"
    assert row["status"] == "pending_review"
    assert row["attached_resume"] == 1
    assert row["subject"] == "Seeking Product Roles at Meesho | Currently Product Intern at Rapido | Ashoka University"
    assert "Meesho" in row["body"]
    assert "Priya" in row["body"]
    assert "+91 9466898689" in row["body"]


def test_generate_pending_emails_respects_attach_resume_setting(settings, story_bank):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        company_id = _insert_company(conn, "Meesho", "consumer")
        _insert_contact(conn, company_id, "Priya Sharma", "priya@meesho.example")
        conn.commit()

        generate_pending_emails(conn, story_bank, settings.templates_dir, "Arjun Khanna", False)
        row = conn.execute("SELECT attached_resume FROM email_queue").fetchone()

    assert row["attached_resume"] == 0


def test_generate_pending_emails_ignores_needs_manual_check_contacts(settings, story_bank):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        company_id = _insert_company(conn, "Meesho", "consumer")
        _insert_contact(
            conn, company_id, "Unverified Person", "maybe@meesho.example", status="needs_manual_check"
        )
        conn.commit()

        stats = generate_pending_emails(
            conn, story_bank, settings.templates_dir, "Arjun Khanna", True
        )

    assert stats.generated == 0
    with connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_queue").fetchone()[0] == 0


def test_generate_pending_emails_is_safe_to_rerun(settings, story_bank):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        company_id = _insert_company(conn, "Meesho", "consumer")
        _insert_contact(conn, company_id, "Priya Sharma", "priya@meesho.example")
        conn.commit()

        first = generate_pending_emails(conn, story_bank, settings.templates_dir, "Arjun Khanna", True)
        second = generate_pending_emails(conn, story_bank, settings.templates_dir, "Arjun Khanna", True)

        count = conn.execute("SELECT COUNT(*) FROM email_queue").fetchone()[0]

    assert first.generated == 1
    assert second.generated == 0
    assert count == 1


def test_generate_pending_emails_skips_missing_niche(settings, story_bank):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        company_id = _insert_company(conn, "No Niche Co", None)
        _insert_contact(conn, company_id, "Someone", "someone@example.com")
        conn.commit()

        stats = generate_pending_emails(
            conn, story_bank, settings.templates_dir, "Arjun Khanna", True
        )

    assert stats.generated == 0
    assert stats.skipped_no_niche == 1


def test_generate_pending_emails_skips_missing_template(settings, story_bank, tmp_path: Path):
    init_db(settings.db_path, settings)
    empty_templates_dir = tmp_path / "templates"
    empty_templates_dir.mkdir()

    with connect(settings.db_path) as conn:
        company_id = _insert_company(conn, "Meesho", "consumer")
        _insert_contact(conn, company_id, "Priya Sharma", "priya@meesho.example")
        conn.commit()

        stats = generate_pending_emails(
            conn, story_bank, empty_templates_dir, "Arjun Khanna", True
        )

    assert stats.generated == 0
    assert stats.skipped_no_template == 1


def test_generate_pending_emails_works_across_all_four_niches(settings, story_bank):
    init_db(settings.db_path, settings)

    with connect(settings.db_path) as conn:
        for i, niche in enumerate(["consumer", "fintech", "data_saas", "ai_devtools"]):
            company_id = _insert_company(conn, f"Co{i}", niche)
            _insert_contact(conn, company_id, f"Person{i}", f"person{i}@co{i}.example")
        conn.commit()

        stats = generate_pending_emails(
            conn, story_bank, settings.templates_dir, "Arjun Khanna", True
        )

        rows = conn.execute(
            "SELECT eq.niche, eq.subject, eq.body, co.name AS company_name "
            "FROM email_queue eq JOIN companies co ON co.id = eq.company_id"
        ).fetchall()

    niches_generated = {row["niche"] for row in rows}
    assert stats.generated == 4
    assert niches_generated == {"consumer", "fintech", "data_saas", "ai_devtools"}

    # niche is still recorded per-row, but every email leads with the Rapido story
    # regardless of niche (2026-08-23 decision) -- same bullets, subject varies only by
    # company name (2026-08-29: company name restored to the subject line).
    for row in rows:
        assert row["subject"] == (
            f"Seeking Product Roles at {row['company_name']} | Currently Product Intern at Rapido | Ashoka University"
        )
        assert "Rapido" in row["body"]
        assert "rides per rider" in row["body"]
