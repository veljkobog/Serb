<#
.SYNOPSIS
  One command to get this machine ready. Safe to re-run.

.DESCRIPTION
  Installs dependencies, creates rotation.json from the example if it is
  missing, and reports what is still needed -- without ever printing a secret.

  This exists because pasting four separate commands kept joining two of them
  into one line, which silently skipped a step and produced a confusing error
  several minutes later.

.EXAMPLE
  .\setup.ps1
#>
param([switch]$SkipInstall)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Show-Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Show-Ok([string]$text)   { Write-Host "   OK   $text" -ForegroundColor Green }
function Show-Bad([string]$text)  { Write-Host "   MISS $text" -ForegroundColor Yellow }

$problems = @()

Show-Step "Python packages"
if ($SkipInstall) {
    Write-Host "   skipped (-SkipInstall)" -ForegroundColor DarkGray
} else {
    python -m pip install -r requirements.txt --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) {
        $problems += "pip install failed - see the output above"
        Show-Bad "dependencies did not install"
    } else {
        Show-Ok "dependencies installed"
    }
}

Show-Step "Rotation config"
if (Test-Path -LiteralPath "rotation.json") {
    Show-Ok "rotation.json already exists (left alone)"
} else {
    Copy-Item "rotation.example.json" "rotation.json"
    Show-Ok "created rotation.json from the example"
}

Show-Step "Credentials"
# Presence only. Printing a token puts it somewhere it does not belong.
if ($env:APOLLO_API_KEY) {
    Show-Ok "APOLLO_API_KEY is set"
} else {
    Show-Bad "APOLLO_API_KEY is not set - no owner names, emails, or website backfill"
    $problems += 'setx APOLLO_API_KEY "<your key>"   then reopen PowerShell'
}
if ($env:HUBSPOT_TOKEN) {
    Show-Ok "HUBSPOT_TOKEN is set"
} else {
    Show-Bad "HUBSPOT_TOKEN is not set - the CRM dedupe will be skipped"
    $problems += 'setx HUBSPOT_TOKEN "<your token>"   then reopen PowerShell'
}

Show-Step "Export folder"
$exportDir = if ($env:LEAD_EXPORT_DIR) {
    $env:LEAD_EXPORT_DIR
} else {
    Join-Path $env:USERPROFILE "ClaudeAssistant\exports"
}
if (Test-Path -LiteralPath $exportDir) {
    Show-Ok $exportDir
} else {
    Show-Bad "$exportDir does not exist"
    $problems += "create $exportDir, or set LEAD_EXPORT_DIR to the real one"
}

Show-Step "Self-test"
python -c "import scraper, daily, enrich_apollo, apollo_people, parse; print('   OK   all modules import')"
if ($LASTEXITCODE -ne 0) { $problems += "the code does not import - dependencies are probably missing" }

Write-Host ""
if ($problems) {
    Write-Host "Not ready yet:" -ForegroundColor Yellow
    $problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "Fix those, reopen PowerShell, and run .\setup.ps1 again." -ForegroundColor DarkGray
    exit 1
}

Write-Host "Ready. Next:  .\daily-leads.ps1 -DryRun" -ForegroundColor Green
exit 0
