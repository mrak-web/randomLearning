"""Email draft generation for the cold-email job agent. See PROJECT.md §4.4.

Only produces drafts (email_queue rows with status='pending_review') — nothing here
touches the Gmail API. Only status='verified' contacts are picked up; contacts flagged
needs_manual_check by contact discovery (§4.3) are deliberately not auto-queued.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent.config import NicheStory


class EmailGenerationError(Exception):
    """Raised when a template or story-bank entry can't be turned into a valid email."""


def get_first_name(full_name: str | None) -> str:
    if not full_name or not full_name.strip():
        return "there"
    return full_name.strip().split()[0]


def build_story_paragraph(story: NicheStory, num_bullets: int = 2) -> str:
    """Stitches the first num_bullets story-bank bullets into a short paragraph."""
    bullets = story.bullets[:num_bullets]
    if not bullets:
        raise EmailGenerationError(f"story bank entry '{story.key}' has no bullets")

    sentences = []
    for i, bullet in enumerate(bullets):
        clause = bullet[0].lower() + bullet[1:]
        if i == 0:
            sentences.append(f"At {story.company_context}, I {clause}.")
        else:
            sentences.append(f"I also {clause}.")
    return " ".join(sentences)


def parse_template(text: str) -> tuple[str, str]:
    """Splits a template file's leading 'Subject: ...' line from the body that follows."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Subject:"):
        raise EmailGenerationError("template must start with a 'Subject:' line")

    subject = lines[0][len("Subject:"):].strip()
    body_lines = lines[1:]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    body = "\n".join(body_lines).rstrip("\n") + "\n"
    return subject, body


def _substitute(template: str, context: dict[str, str]) -> str:
    result = template
    for key, value in context.items():
        result = result.replace("{{" + key + "}}", value)
    if "{{" in result:
        raise EmailGenerationError(f"unresolved placeholder in rendered template: {result!r}")
    return result


def render_email(
    template_text: str,
    *,
    company_name: str,
    contact_name: str | None,
    story: NicheStory,
    sender_display_name: str,
) -> tuple[str, str]:
    subject_template, body_template = parse_template(template_text)
    context = {
        "company_name": company_name,
        "contact_first_name": get_first_name(contact_name),
        "story_paragraph": build_story_paragraph(story),
        "sender_name": sender_display_name,
    }
    subject = _substitute(subject_template, context)
    body = _substitute(body_template, context)
    return subject, body


@dataclass(frozen=True)
class GenerationStats:
    generated: int = 0
    skipped_no_niche: int = 0
    skipped_no_template: int = 0


def generate_pending_emails(
    conn: sqlite3.Connection,
    story_bank: dict[str, NicheStory],
    templates_dir: Path,
    sender_display_name: str,
    attach_resume_by_default: bool,
) -> GenerationStats:
    """Drafts an initial email for every verified contact that doesn't have one yet."""
    rows = conn.execute(
        """
        SELECT ct.id AS contact_id, ct.name AS contact_name, ct.company_id,
               co.name AS company_name, co.niche
        FROM contacts ct
        JOIN companies co ON co.id = ct.company_id
        LEFT JOIN email_queue eq ON eq.contact_id = ct.id AND eq.kind = 'initial'
        WHERE ct.status = 'verified' AND eq.id IS NULL
        """
    ).fetchall()

    generated = 0
    skipped_no_niche = 0
    skipped_no_template = 0

    for row in rows:
        niche = row["niche"]
        if not niche or niche not in story_bank:
            skipped_no_niche += 1
            continue

        template_path = templates_dir / f"{niche}.txt"
        if not template_path.exists():
            skipped_no_template += 1
            continue

        template_text = template_path.read_text(encoding="utf-8")
        subject, body = render_email(
            template_text,
            company_name=row["company_name"],
            contact_name=row["contact_name"],
            story=story_bank[niche],
            sender_display_name=sender_display_name,
        )

        conn.execute(
            """
            INSERT INTO email_queue
                (contact_id, company_id, niche, subject, body, kind, attached_resume, status)
            VALUES (?, ?, ?, ?, ?, 'initial', ?, 'pending_review')
            """,
            (
                row["contact_id"],
                row["company_id"],
                niche,
                subject,
                body,
                int(attach_resume_by_default),
            ),
        )
        generated += 1

    conn.commit()
    return GenerationStats(
        generated=generated,
        skipped_no_niche=skipped_no_niche,
        skipped_no_template=skipped_no_template,
    )
