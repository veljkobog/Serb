<#
.SYNOPSIS
  Register (or remove) the weekday 9am lead run in Task Scheduler.

.DESCRIPTION
  Creates a task that runs daily-leads.ps1 Monday-Friday at 9am, waking the
  machine if it is asleep. It cannot run on a machine that is powered off.

.EXAMPLE
  .\install-schedule.ps1
  .\install-schedule.ps1 -At 08:30
  .\install-schedule.ps1 -Remove
#>
param(
    [string]$At = "09:00",
    [string]$TaskName = "TotalityLeadRun",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Yellow
    exit 0
}

$script = Join-Path $PSScriptRoot "daily-leads.ps1"
if (-not (Test-Path -LiteralPath $script)) {
    Write-Host "daily-leads.ps1 not found next to this script." -ForegroundColor Red
    exit 2
}

# Check the prerequisites now rather than discovering them at 9am on a
# morning nobody is watching.
$missing = @()
if (-not $env:APOLLO_API_KEY) { $missing += "APOLLO_API_KEY (owner names and emails)" }
if (-not $env:HUBSPOT_TOKEN)  { $missing += "HUBSPOT_TOKEN (CRM dedupe)" }
$config = Join-Path $PSScriptRoot "rotation.json"
if (-not (Test-Path -LiteralPath $config)) {
    $missing += "rotation.json (copy rotation.example.json and edit the metros)"
}
if ($missing) {
    Write-Host "Not ready to schedule yet:" -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "The task would run and fail every morning. Fix these first." -ForegroundColor DarkGray
    exit 2
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At

$settings = New-ScheduledTaskSettings `
    -WakeToRun `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily BBB lead pull, enrichment and CRM check" `
    -Force | Out-Null

Write-Host "Scheduled '$TaskName' for $At, Monday-Friday." -ForegroundColor Green
Write-Host ""
Write-Host "  Test it now:     Start-ScheduledTask -TaskName $TaskName" -ForegroundColor DarkGray
Write-Host "  See its state:   Get-ScheduledTaskInfo -TaskName $TaskName" -ForegroundColor DarkGray
Write-Host "  Remove it:       .\install-schedule.ps1 -Remove" -ForegroundColor DarkGray
Write-Host ""
Write-Host "StartWhenAvailable is on, so a missed run (laptop shut) fires when you" -ForegroundColor DarkGray
Write-Host "next log in. It cannot run while the machine is off." -ForegroundColor DarkGray
