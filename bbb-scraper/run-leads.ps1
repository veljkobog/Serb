<#
.SYNOPSIS
  Pull a lead list from BBB and drop it in the leads folder, ready to review.

.EXAMPLE
  .\run-leads.ps1 -Category plumber -Location wichita-ks
  .\run-leads.ps1 -Category plumber,roofing-contractors -Location nc -MinYears 10 -Max 500

.NOTES
  Output goes to ..\leads\<category>-<location>-<date>.csv and the folder opens
  when the run finishes. Nothing is overwritten -- each run is dated.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Category,
    [Parameter(Mandatory = $true)][string]$Location,
    [int]$Max = 100,
    [int]$MinYears = 0,
    [string]$LeadsFolder = "$PSScriptRoot\..\leads",
    [switch]$NoOpen,
    [switch]$Browser
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $LeadsFolder)) { New-Item -ItemType Directory -Path $LeadsFolder | Out-Null }

$stamp    = Get-Date -Format "yyyy-MM-dd"
$catLabel = ($Category -join "+")
$out      = Join-Path $LeadsFolder "$catLabel-$Location-$stamp.csv"
$report   = Join-Path $LeadsFolder "$catLabel-$Location-$stamp.json"

$args = @(
    "scraper.py",
    "--category", ($Category -join ","),
    "--location", $Location,
    "--max-results", $Max,
    "--output", $out,
    "--report", $report,
    "--column-map", "$PSScriptRoot\lead-format.json",
    "-v"
)
if ($MinYears -gt 0) { $args += @("--min-years", $MinYears) }
if ($Browser)        { $args += "--browser" }

Write-Host "Pulling $catLabel in $Location (cap $Max)..." -ForegroundColor Cyan
Write-Host "Output: $out`n" -ForegroundColor DarkGray

python @args
$code = $LASTEXITCODE

if ($code -eq 0 -and (Test-Path $out)) {
    $rows = (Import-Csv $out).Count
    Write-Host "`n$rows leads written to $out" -ForegroundColor Green
    Write-Host "Next: python crm_check.py `"$out`"  (needs `$env:HUBSPOT_TOKEN)" -ForegroundColor DarkGray
    if (-not $NoOpen) { explorer $LeadsFolder }
} else {
    Write-Host "`nRun did not produce a file - see the messages above." -ForegroundColor Yellow
}
exit $code
