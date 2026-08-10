"""Gmail sending for the cold-email job agent. See PROJECT.md §4.6.

GmailApiSender wraps the Gmail API (users.messages.send) rather than raw SMTP, so
thread/reply tracking (§4.7) comes for free. Requires a one-time OAuth flow
(scripts/gmail_auth.py) that writes gmail.token_path (gitignored, never committed).

google-api-python-client / google-auth-oauthlib are imported lazily inside
GmailApiSender.__init__ rather than at module level, so this module — and anything
that imports agent (which re-exports build_mime_message for testing) — doesn't hard-
require those packages just to build/inspect a MIME message.
"""

from __future__ import annotations

import base64
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from agent.sending import EmailSender, SendingError

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def build_mime_message(
    *,
    sender_email: str,
    to_email: str,
    subject: str,
    body: str,
    resume_path: Path | None,
) -> MIMEMultipart:
    message = MIMEMultipart()
    message["To"] = to_email
    message["From"] = sender_email
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    if resume_path is not None:
        if not resume_path.exists():
            raise SendingError(f"resume PDF not found: {resume_path}")
        attachment = MIMEApplication(resume_path.read_bytes(), _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=resume_path.name)
        message.attach(attachment)

    return message


def encode_message(message: MIMEMultipart) -> dict:
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return {"raw": raw}


class GmailApiSender(EmailSender):
    """Requires gmail.token_path from scripts/gmail_auth.py's one-time OAuth flow."""

    def __init__(self, sender_email: str, token_path: Path):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise SendingError(
                "google-api-python-client and google-auth-oauthlib are required for "
                "GmailApiSender; run: pip install -r requirements.txt"
            ) from exc

        if not token_path.exists():
            raise SendingError(
                f"Gmail token not found at {token_path}. Run scripts/gmail_auth.py first."
            )

        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not credentials.valid:
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                token_path.write_text(credentials.to_json(), encoding="utf-8")
            else:
                raise SendingError(
                    f"Gmail token at {token_path} is invalid and can't be refreshed. "
                    "Run scripts/gmail_auth.py again."
                )

        self.sender_email = sender_email
        self.service = build("gmail", "v1", credentials=credentials)

    def send(self, to_email: str, subject: str, body: str, resume_path: Path | None) -> str:
        from googleapiclient.errors import HttpError

        message = build_mime_message(
            sender_email=self.sender_email,
            to_email=to_email,
            subject=subject,
            body=body,
            resume_path=resume_path,
        )
        try:
            result = (
                self.service.users()
                .messages()
                .send(userId="me", body=encode_message(message))
                .execute()
            )
        except HttpError as exc:
            raise SendingError(f"Gmail send failed for {to_email}: {exc}") from exc

        return result["threadId"]
