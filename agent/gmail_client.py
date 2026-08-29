"""Gmail sending + reply/bounce checking for the cold-email job agent. See PROJECT.md
§4.6-4.7.

GmailApiSender wraps the Gmail API (users.messages.send) rather than raw SMTP, so
thread/reply tracking (§4.7) comes for free. Requires a one-time OAuth flow
(scripts/gmail_auth.py) that writes gmail.token_path (gitignored, never committed).

google-api-python-client / google-auth-oauthlib are imported lazily inside
GmailApiSender.__init__ / GmailReplyChecker.__init__ rather than at module level, so
this module — and anything that imports agent (which re-exports build_mime_message for
testing) — doesn't hard-require those packages just to build/inspect a MIME message.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from agent.sending import EmailSender, SendingError

# gmail.send: sending. gmail.readonly: module 8's reply/bounce checking (threads().get,
# messages().list) added alongside it -- both scopes live on one token since both
# GmailApiSender and GmailReplyChecker authenticate against the same account.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def build_mime_message(
    *,
    sender_email: str,
    to_email: str,
    subject: str,
    body: str,
    resume_path: Path | None,
    in_reply_to_message_id: str | None = None,
) -> MIMEMultipart:
    message = MIMEMultipart()
    message["To"] = to_email
    message["From"] = sender_email
    message["Subject"] = subject
    if in_reply_to_message_id is not None:
        message["In-Reply-To"] = in_reply_to_message_id
        message["References"] = in_reply_to_message_id
    message.attach(MIMEText(body, "plain"))

    if resume_path is not None:
        if not resume_path.exists():
            raise SendingError(f"resume PDF not found: {resume_path}")
        attachment = MIMEApplication(resume_path.read_bytes(), _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=resume_path.name)
        message.attach(attachment)

    return message


def encode_message(message: MIMEMultipart, thread_id: str | None = None) -> dict:
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    body: dict = {"raw": raw}
    if thread_id is not None:
        body["threadId"] = thread_id
    return body


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

    def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        resume_path: Path | None,
        thread_id: str | None = None,
    ) -> str:
        """Sends one email. When thread_id is given (a follow-up), the message is
        threaded onto the original conversation: subject gets a 'Re: ' prefix,
        In-Reply-To/References are set from the thread's last message, and threadId is
        passed to the API -- all three matter for Gmail to actually group it, not just
        the threadId alone.
        """
        from googleapiclient.errors import HttpError

        in_reply_to = None
        if thread_id is not None:
            subject, in_reply_to = self._threading_context(thread_id, subject)

        message = build_mime_message(
            sender_email=self.sender_email,
            to_email=to_email,
            subject=subject,
            body=body,
            resume_path=resume_path,
            in_reply_to_message_id=in_reply_to,
        )
        try:
            result = (
                self.service.users()
                .messages()
                .send(userId="me", body=encode_message(message, thread_id=thread_id))
                .execute()
            )
        except HttpError as exc:
            raise SendingError(f"Gmail send failed for {to_email}: {exc}") from exc

        return result["threadId"]

    def _threading_context(self, thread_id: str, subject: str) -> tuple[str, str | None]:
        """Looks up the thread's last message to get a Re:-prefixed subject and its
        Message-ID header (for In-Reply-To/References) -- fetched live rather than
        stored on the email_queue row, so no extra column is needed to track it.
        """
        from googleapiclient.errors import HttpError

        try:
            thread = (
                self.service.users()
                .threads()
                .get(
                    userId="me",
                    id=thread_id,
                    format="metadata",
                    metadataHeaders=["Message-ID", "Subject"],
                )
                .execute()
            )
        except HttpError as exc:
            raise SendingError(f"Gmail thread lookup failed for {thread_id}: {exc}") from exc

        messages = thread.get("messages", [])
        if not messages:
            return subject, None

        headers = {h["name"]: h["value"] for h in messages[-1]["payload"].get("headers", [])}
        message_id = headers.get("Message-ID")
        original_subject = headers.get("Subject", subject)
        threaded_subject = (
            original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
        )
        return threaded_subject, message_id


class ReplyChecker(ABC):
    """A pluggable reply/bounce detection backend. Gmail is the only implementation
    today. See PROJECT.md §4.7.
    """

    @abstractmethod
    def thread_has_reply(self, thread_id: str, sender_email: str) -> bool:
        """True if the thread's last message wasn't sent by sender_email -- i.e. the
        contact (or someone else on the thread) replied.
        """

    @abstractmethod
    def has_bounce(self, recipient_email: str, after: date) -> bool:
        """True if a delivery-failure notification for recipient_email arrived in the
        sender's own inbox on/after `after`. Bounce mail doesn't land in the original
        thread, so this is a separate inbox search, not a thread check.
        """


class GmailReplyChecker(ReplyChecker):
    """Requires gmail.token_path from scripts/gmail_auth.py's one-time OAuth flow
    (needs the gmail.readonly scope added in this module -- re-run gmail_auth.py if an
    existing token predates that scope).
    """

    def __init__(self, token_path: Path):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise SendingError(
                "google-api-python-client and google-auth-oauthlib are required for "
                "GmailReplyChecker; run: pip install -r requirements.txt"
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

        self.service = build("gmail", "v1", credentials=credentials)

    def thread_has_reply(self, thread_id: str, sender_email: str) -> bool:
        from googleapiclient.errors import HttpError

        try:
            thread = (
                self.service.users()
                .threads()
                .get(userId="me", id=thread_id, format="metadata", metadataHeaders=["From"])
                .execute()
            )
        except HttpError as exc:
            raise SendingError(f"Gmail thread lookup failed for {thread_id}: {exc}") from exc

        messages = thread.get("messages", [])
        if len(messages) < 2:
            return False

        headers = {h["name"]: h["value"] for h in messages[-1]["payload"].get("headers", [])}
        last_from = headers.get("From", "")
        return sender_email.lower() not in last_from.lower()

    def has_bounce(self, recipient_email: str, after: date) -> bool:
        from googleapiclient.errors import HttpError

        query = f'from:mailer-daemon "{recipient_email}" after:{after.isoformat()}'
        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=1)
                .execute()
            )
        except HttpError as exc:
            raise SendingError(f"Gmail bounce search failed for {recipient_email}: {exc}") from exc

        return bool(result.get("messages"))
