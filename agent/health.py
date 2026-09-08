"""Operational health checks for the cold-email pipeline's unattended parts -- the
Gmail OAuth token and the Windows Task Scheduler entry that actually triggers sends.
Surfaced on the dashboard's Tracking tab so a dead token or a misconfigured task shows
up before a send is silently missed, rather than after (see PROJECT.md incident,
2026-09-08: a task logon-mode mismatch caused a full daily miss with no dashboard
signal until Arjun noticed the next day).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GmailTokenStatus:
    ok: bool
    detail: str


def check_gmail_token(token_path: Path, scopes: list[str]) -> GmailTokenStatus:
    """Live refresh check -- the only reliable way to know the token is usable right
    now, since a Google OAuth app left in Testing mode can have its refresh token die
    at any point regardless of file timestamps (re-running gmail_auth.py just resets
    that countdown, it doesn't remove it).
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return GmailTokenStatus(ok=False, detail="google-auth-oauthlib not installed")

    if not token_path.exists():
        return GmailTokenStatus(
            ok=False, detail=f"No token at {token_path} -- run scripts/gmail_auth.py"
        )

    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), scopes)
        if not credentials.valid:
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                token_path.write_text(credentials.to_json(), encoding="utf-8")
            else:
                return GmailTokenStatus(
                    ok=False,
                    detail="Token invalid with no refresh_token -- run scripts/gmail_auth.py",
                )
    except Exception as exc:  # noqa: BLE001 -- refresh can raise RefreshError, network
        # errors, etc.; any of them means "not usable right now", which is what this
        # check exists to report, not to classify.
        return GmailTokenStatus(
            ok=False,
            detail=(
                f"Refresh failed: {exc}. Usually means the Google OAuth consent screen "
                "is still in Testing mode (7-day refresh token expiry) -- publish it to "
                "Production in Google Cloud Console, then re-run scripts/gmail_auth.py."
            ),
        )

    return GmailTokenStatus(ok=True, detail="Valid")


@dataclass
class SchedulerStatus:
    ok: bool
    detail: str
    next_run_time: str | None = None
    last_run_time: str | None = None
    last_result: str | None = None
    logon_mode: str | None = None


# Only the codes this pipeline has actually hit are decoded by name; anything else
# falls back to "non-zero result, check manually" rather than guessing.
_RESULT_MESSAGES = {
    "-2147020576": (
        "failed to start -- logon mode requires an interactive session that wasn't "
        "available, so the task likely didn't run at all"
    ),
}


def parse_schtasks_verbose(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def check_scheduler_status(task_name: str) -> SchedulerStatus:
    """Shells out to schtasks (Windows-only, ships with the OS) since there's no
    stdlib way to query Task Scheduler. Never raises -- a query failure (task missing,
    not on Windows, schtasks unavailable) is reported as a status, not an exception, so
    a dashboard page load never breaks over this check.
    """
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/v", "/fo", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SchedulerStatus(ok=False, detail=f"Could not query Task Scheduler: {exc}")

    if result.returncode != 0:
        return SchedulerStatus(
            ok=False,
            detail=f"Task '{task_name}' not found or schtasks failed: {result.stderr.strip()}",
        )

    fields = parse_schtasks_verbose(result.stdout)
    logon_mode = fields.get("Logon Mode")
    last_result = fields.get("Last Result")
    last_run = fields.get("Last Run Time")
    next_run = fields.get("Next Run Time")

    problems = []
    # schtasks reports the broken (Interactive-logon-type) case as exactly
    # "Interactive only". S4U shows as "Interactive/Background" -- despite also
    # containing "interactive", that one runs fine unattended, so this must be an
    # equality check against the known-bad string, not a substring match.
    if logon_mode and logon_mode.strip().lower() == "interactive only":
        problems.append(
            "logon mode is 'Interactive only' -- the task won't run unless "
            "you're actively logged on at trigger time; switch it to S4U"
        )
    if last_result and last_result != "0":
        decoded = _RESULT_MESSAGES.get(last_result, "non-zero result, check manually")
        problems.append(f"last run result {last_result} ({decoded})")

    ok = not problems
    return SchedulerStatus(
        ok=ok,
        detail="Healthy" if ok else "; ".join(problems),
        next_run_time=next_run,
        last_run_time=last_run,
        last_result=last_result,
        logon_mode=logon_mode,
    )
