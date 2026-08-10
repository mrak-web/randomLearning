"""One-time Gmail OAuth setup. See PROJECT.md §4.6.

Usage: py scripts/gmail_auth.py

Needs a Google Cloud OAuth client (Desktop app type): create one at
https://console.cloud.google.com/apis/credentials, download its JSON, and save it at
the path in settings.yaml's gmail.credentials_path (default: data/gmail_credentials.json,
gitignored). This opens a browser for you to approve access, then writes
gmail.token_path (default: data/gmail_token.json) — GmailApiSender reads that token on
every send; no further auth needed until it expires, and it refreshes itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import SCOPES, load_settings


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("google-auth-oauthlib is required. Run: pip install -r requirements.txt")
        raise SystemExit(1)

    settings = load_settings()
    if not settings.gmail_credentials_path.exists():
        print(f"Gmail OAuth client JSON not found at {settings.gmail_credentials_path}.")
        print("Create one at https://console.cloud.google.com/apis/credentials")
        print("(OAuth client ID -> Desktop app), download it, and save it there.")
        raise SystemExit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(settings.gmail_credentials_path), SCOPES)
    credentials = flow.run_local_server(port=0)

    settings.gmail_token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.gmail_token_path.write_text(credentials.to_json(), encoding="utf-8")
    print(f"Saved Gmail token to {settings.gmail_token_path}")


if __name__ == "__main__":
    main()
