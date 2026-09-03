<#
.SYNOPSIS
  The 9am run. Pulls today's lists, enriches them, and reports.

.DESCRIPTION
  Wraps daily.py for Task Scheduler. Everything it needs comes from the
  environment (APOLLO_API_KEY, HUBSPOT_TOKEN, optionally LEAD_EXPORT_DIR), so
  the scheduled task itself carries no secrets.

  Register it to run every weekday at 9am with:
      .\install-schedule.ps1

.EXAMPLE
  .\daily-leads.ps1 -DryRun        # show today's plan, fetch nothing
  .\daily-leads.ps1 -Date 2026-09-07
#>
param(
    [switch]$DryRun,
    [string]$Date,
    [string]$ExportFolder
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not $ExportFolder) {
    $ExportFolder = if ($env:LEAD_EXPORT_DIR) {
        $env:LEAD_EXPORT_DIR
    } else {
        Join-Path $env:USERPROFILE "ClaudeAssistant\exports"
    }
}

$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$log = Join-Path $logDir ("run-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

$dailyArgs = @("daily.py", "--export-dir", $ExportFolder)
if ($DryRun) { $dailyArgs += "--dry-run" }
if ($Date)   { $dailyArgs += @("--date", $Date) }

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $log -Append -Encoding utf8
python @dailyArgs 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE

# Toast on failure only. A notification every single morning trains you to
# dismiss it without reading, which is worse than no notification at all.
if ($code -ne 0) {
    "EXIT $code - see $log" | Out-File -FilePath $log -Append -Encoding utf8
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Warning
        $icon.Visible = $true
        $icon.ShowBalloonTip(20000, "Lead run needs attention",
            "Check ATTENTION-$(Get-Date -Format 'yyyy-MM-dd').txt in your exports folder.",
            [System.Windows.Forms.ToolTipIcon]::Warning)
        Start-Sleep -Seconds 12
        $icon.Dispose()
    } catch {
        Write-Host "(could not show a notification: $_)" -ForegroundColor DarkGray
    }
    Write-Host "`nRun finished with problems. See:" -ForegroundColor Yellow
    Write-Host "  $log" -ForegroundColor Yellow
    Write-Host "  $ExportFolder\ATTENTION-$(Get-Date -Format 'yyyy-MM-dd').txt" -ForegroundColor Yellow
} else {
    Write-Host "`nDone. Sheets are in $ExportFolder" -ForegroundColor Green
}

exit $code
