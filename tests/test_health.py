from __future__ import annotations

from pathlib import Path

from agent.health import (
    check_gmail_token,
    check_scheduler_status,
    parse_schtasks_verbose,
)

SCHTASKS_OUTPUT_HEALTHY = """Folder: \\
HostName:                             PC
TaskName:                             \\ColdEmailAgent_DailyRun
Next Run Time:                        09-09-2026 10:30:00
Status:                               Ready
Logon Mode:                           S4U
Last Run Time:                        08-09-2026 10:30:03
Last Result:                          0
"""

SCHTASKS_OUTPUT_INTERACTIVE_FAILURE = """Folder: \\
HostName:                             PC
TaskName:                             \\ColdEmailAgent_DailyRun
Next Run Time:                        09-09-2026 10:30:00
Status:                               Ready
Logon Mode:                           Interactive only
Last Run Time:                        08-09-2026 22:29:51
Last Result:                          -2147020576
"""


def test_parse_schtasks_verbose_splits_on_first_colon():
    fields = parse_schtasks_verbose(SCHTASKS_OUTPUT_HEALTHY)
    assert fields["Logon Mode"] == "S4U"
    assert fields["Last Result"] == "0"
    assert fields["Next Run Time"] == "09-09-2026 10:30:00"


def test_check_scheduler_status_ok_when_s4u_and_success(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = SCHTASKS_OUTPUT_HEALTHY
        stderr = ""

    monkeypatch.setattr("agent.health.subprocess.run", lambda *a, **k: FakeResult())

    status = check_scheduler_status("ColdEmailAgent_DailyRun")
    assert status.ok is True
    assert status.logon_mode == "S4U"


def test_check_scheduler_status_flags_interactive_logon_mismatch(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = SCHTASKS_OUTPUT_INTERACTIVE_FAILURE
        stderr = ""

    monkeypatch.setattr("agent.health.subprocess.run", lambda *a, **k: FakeResult())

    status = check_scheduler_status("ColdEmailAgent_DailyRun")
    assert status.ok is False
    assert "Interactive only" in status.detail
    assert "-2147020576" in status.detail


def test_check_scheduler_status_task_not_found(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "ERROR: The system cannot find the file specified."

    monkeypatch.setattr("agent.health.subprocess.run", lambda *a, **k: FakeResult())

    status = check_scheduler_status("NoSuchTask")
    assert status.ok is False
    assert "not found" in status.detail


def test_check_gmail_token_missing_file(tmp_path: Path):
    status = check_gmail_token(tmp_path / "no_token.json", ["scope"])
    assert status.ok is False
    assert "gmail_auth.py" in status.detail
