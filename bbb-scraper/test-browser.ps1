<#
.SYNOPSIS
  Does a real browser get the BBB profile pages that plain HTTP cannot?

.DESCRIPTION
  Profile pages carry the employee count, and headcount is the whole size
  screen. Over plain HTTP they answer 403. This pulls a small sample through
  Playwright and reports whether the headcount actually arrived -- which is
  the only question that matters, and not one you can answer by reading a
  "run completed" line.

  Deliberately small: 5 results, one vertical, one metro. A couple of minutes.

.EXAMPLE
  .\test-browser.ps1
  .\test-browser.ps1 -Category plumber -Location atlanta-ga
#>
param(
    [string]$Category = "roofing-contractors",
    [string]$Location = "houston-tx",
    [int]$Count = 5
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$out = Join-Path $PSScriptRoot "browser-test.csv"
if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }

Write-Host "Pulling $Count $Category in $Location through a real browser..." -ForegroundColor Cyan
Write-Host "(a couple of minutes -- Chromium starts, then pages load one at a time)`n" -ForegroundColor DarkGray

$testArgs = @(
    "scraper.py",
    "--category", $Category,
    "--location", $Location,
    "--browser",
    "--max-results", $Count,
    "--output", $out,
    "--no-resume",
    "-v"
)

$ErrorActionPreference = "Continue"
& python @testArgs 2>&1 | ForEach-Object { $_.ToString() }
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $out)) {
    Write-Host "NO FILE WRITTEN - the scrape itself failed (exit $code)." -ForegroundColor Red
    Write-Host "Paste everything above; the browser never got started or the" -ForegroundColor DarkGray
    Write-Host "search pages were blocked, which is a different problem." -ForegroundColor DarkGray
    exit 1
}

$rows = @(Import-Csv $out)
if ($rows.Count -eq 0) {
    Write-Host "FILE IS EMPTY - no listings survived. Paste the output above." -ForegroundColor Red
    exit 1
}

$withHeadcount = @($rows | Where-Object { $_.employees -and $_.employees.Trim() -ne "" })
$withWebsite   = @($rows | Where-Object { $_.website   -and $_.website.Trim()   -ne "" })
$withYears     = @($rows | Where-Object { $_.years_in_business -and $_.years_in_business.Trim() -ne "" })

Write-Host "$($rows.Count) row(s) pulled" -ForegroundColor White
Write-Host "  headcount : $($withHeadcount.Count)/$($rows.Count)"
Write-Host "  website   : $($withWebsite.Count)/$($rows.Count)"
Write-Host "  years     : $($withYears.Count)/$($rows.Count)"
Write-Host ""

if ($withHeadcount.Count -gt 0) {
    Write-Host "PROFILE PAGES LOADED. The size screen can run." -ForegroundColor Green
    Write-Host "Sample:" -ForegroundColor DarkGray
    $rows | Select-Object -First 5 company_name, employees, years_in_business, website |
        Format-Table -AutoSize
} else {
    Write-Host "PROFILE PAGES STILL BLOCKED - headcount is empty on every row." -ForegroundColor Yellow
    Write-Host "The browser did not beat it either. Paste this whole output." -ForegroundColor DarkGray
}
Write-Host "======================================================" -ForegroundColor Cyan
