# Register the Tuesday-night ingestion job on the home server (v2 step 6).
#
# Most chains refresh weekly ads on Wednesdays, so the pull runs Tuesday
# 21:30 local time via Windows Task Scheduler (schtasks, per the spec — no
# orchestration framework). Run once from an elevated-or-not PowerShell on
# the UM870, from anywhere:
#
#   powershell -ExecutionPolicy Bypass -File ops\register_weekly_pull.ps1
#
# Remove with:
#
#   powershell -ExecutionPolicy Bypass -File ops\register_weekly_pull.ps1 -Unregister
#
# The task runs `uv run grocery-weekly-pull` in the repo root (so the
# project .env is picked up) and appends stdout/stderr to
# data\logs\schtask.log; per-stage status also lands in
# data\logs\weekly_pull.log either way.

param(
    [switch]$Unregister,
    [string]$TaskName = "GroceryOptimizer Weekly Pull",
    [string]$StartTime = "21:30"
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    schtasks /Delete /TN $TaskName /F
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

# Repo root = parent of the ops/ directory this script lives in.
$RepoRoot = Split-Path -Parent $PSScriptRoot

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Warning "uv not found on PATH; the task will fail until uv is installed for this user."
}

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "data\logs") | Out-Null

# cmd /c so `cd` plus the append-redirect work inside a single task action.
$Action = "cmd /c cd /d `"$RepoRoot`" && uv run grocery-weekly-pull >> data\logs\schtask.log 2>&1"

schtasks /Create /F /TN $TaskName /TR $Action /SC WEEKLY /D TUE /ST $StartTime
Write-Host ""
Write-Host "Registered '$TaskName': Tuesdays $StartTime, repo $RepoRoot"
Write-Host "Dry-run it now with:  schtasks /Run /TN `"$TaskName`""
Write-Host "Inspect with:         schtasks /Query /TN `"$TaskName`" /V /FO LIST"
