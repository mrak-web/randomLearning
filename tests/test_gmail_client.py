from __future__ import annotations

import base64
from email import message_from_bytes
from pathlib import Path

import pytest

from agent.gmail_client import build_mime_message, encode_message
from agent.sending import SendingError

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_RESUME = REPO_ROOT / "tests" / "fixtures" / "fake_resume.pdf"


def test_build_mime_message_without_resume_has_headers_and_body():
    message = build_mime_message(
        sender_email="arjun@example.com",
        to_email="priya@meesho.example",
        subject="Product role at Meesho",
        body="Hi Priya, ...",
        resume_path=None,
    )

    assert message["To"] == "priya@meesho.example"
    assert message["From"] == "arjun@example.com"
    assert message["Subject"] == "Product role at Meesho"

    parts = message.get_payload()
    assert len(parts) == 1
    assert parts[0].get_content_type() == "text/plain"
    assert parts[0].get_payload() == "Hi Priya, ..."


def test_build_mime_message_with_resume_attaches_pdf():
    message = build_mime_message(
        sender_email="arjun@example.com",
        to_email="priya@meesho.example",
        subject="Product role at Meesho",
        body="Hi Priya, ...",
        resume_path=FAKE_RESUME,
    )

    parts = message.get_payload()
    assert len(parts) == 2
    text_part, attachment_part = parts
    assert text_part.get_content_type() == "text/plain"
    assert attachment_part.get_content_type() == "application/pdf"
    assert attachment_part.get_filename() == "fake_resume.pdf"

    attached_bytes = attachment_part.get_payload(decode=True)
    assert attached_bytes == FAKE_RESUME.read_bytes()


def test_build_mime_message_raises_when_resume_missing(tmp_path: Path):
    missing = tmp_path / "does_not_exist.pdf"

    with pytest.raises(SendingError, match="not found"):
        build_mime_message(
            sender_email="arjun@example.com",
            to_email="priya@meesho.example",
            subject="Subject",
            body="Body",
            resume_path=missing,
        )


def test_encode_message_round_trips_through_base64():
    message = build_mime_message(
        sender_email="arjun@example.com",
        to_email="priya@meesho.example",
        subject="Product role at Meesho",
        body="Hi Priya, this is the body.",
        resume_path=FAKE_RESUME,
    )

    encoded = encode_message(message)

    assert set(encoded.keys()) == {"raw"}
    raw_bytes = base64.urlsafe_b64decode(encoded["raw"])
    parsed = message_from_bytes(raw_bytes)

    assert parsed["To"] == "priya@meesho.example"
    assert parsed["From"] == "arjun@example.com"
    assert parsed["Subject"] == "Product role at Meesho"

    text_part = parsed.get_payload()[0]
    assert text_part.get_payload() == "Hi Priya, this is the body."


def test_build_mime_message_sets_threading_headers_when_given():
    message = build_mime_message(
        sender_email="arjun@example.com",
        to_email="priya@meesho.example",
        subject="Re: Product role at Meesho",
        body="Just bumping this.",
        resume_path=None,
        in_reply_to_message_id="<original@mail.gmail.com>",
    )

    assert message["In-Reply-To"] == "<original@mail.gmail.com>"
    assert message["References"] == "<original@mail.gmail.com>"


def test_build_mime_message_omits_threading_headers_by_default():
    message = build_mime_message(
        sender_email="arjun@example.com",
        to_email="priya@meesho.example",
        subject="Product role at Meesho",
        body="Hi Priya, ...",
        resume_path=None,
    )

    assert message["In-Reply-To"] is None
    assert message["References"] is None


def test_encode_message_includes_thread_id_when_given():
    message = build_mime_message(
        sender_email="arjun@example.com",
        to_email="priya@meesho.example",
        subject="Product role at Meesho",
        body="Hi Priya, ...",
        resume_path=None,
    )

    encoded = encode_message(message, thread_id="thread-123")

    assert encoded["threadId"] == "thread-123"
    assert set(encoded.keys()) == {"raw", "threadId"}
