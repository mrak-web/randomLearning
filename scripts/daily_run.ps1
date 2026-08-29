# Daily automation entry point for the cold-email job agent -- the "delivery clock".
# Runs check_replies.py (bounce/reply detection + follow-up generation) then
# send_batch.py (sends whatever is currently status='approved'), in that order.
#
# Registered as a Windows Task Scheduler entry firing once daily at a fixed time
# (10:30am) -- see PROJECT.md SS4.6/SS4.7. This is deliberately the ONLY thing that
# fixes send timing: reviewing/approving in the Streamlit dashboard can happen
# whenever (8pm, whenever), but nothing actually leaves the outbox until this task
# runs, regardless of when a draft was approved.
#
# Not meant to be run interactively -- use `py scripts/check_replies.py` /
# `py scripts/send_batch.py` directly, or the dashboard's "Check Replies & Follow-ups
# Now" / "Send Now" buttons, for on-demand runs.

$ErrorActionPreference = "Continue"
Set-Location "E:\VSCode"

$logFile = "E:\VSCode\data\daily_run.log"
"=== Daily run started: $(Get-Date -Format o) ===" | Out-File -FilePath $logFile -Append -Encoding utf8

# Only stdout is piped through -- redirecting a native command's stderr in
# PowerShell 5.1 wraps each line as a NativeCommandError and can mangle encoding.
# Non-fatal warnings on stderr (e.g. a Python deprecation notice) are simply
# dropped here rather than logged, which is fine for this unattended run.
py scripts\check_replies.py | Out-File -FilePath $logFile -Append -Encoding utf8
py scripts\send_batch.py | Out-File -FilePath $logFile -Append -Encoding utf8

"=== Daily run finished: $(Get-Date -Format o) ===" | Out-File -FilePath $logFile -Append -Encoding utf8
