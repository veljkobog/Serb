<#
.SYNOPSIS
  Pull a lead list from BBB into the exports folder BOB sources from.

.EXAMPLE
  .\run-leads.ps1 -Category plumber -Location wichita-ks
  .\run-leads.ps1 -Category roofing-contractors -Location wichita-ks -Target 15
  .\run-leads.ps1 -Category plumber -Location nc -MinYears 10 -Max 500 -KeepSiteless

.NOTES
  Default destination is <your user folder>\ClaudeAssistant\exports, i.e.
  C:\Users\<you>\ClaudeAssistant\exports. Override per run with -ExportFolder,
  or permanently with:  setx LEAD_EXPORT_DIR "D:\some\path"

  -Target is the number of rows you want on the finished sheet (default 15).
  -Max is how many raw BBB listings to pull before filtering; keep it well
  above -Target or filtering will leave you short.

  Companies with no website are dropped by default, using Apollo's free
  lookup to find the website BBB withholds. Pass -KeepSiteless to keep them,
  or -NoApollo to skip the lookup entirely.

  The folder must already exist. If it doesn't, the run stops rather than
  creating one -- a lead file written to a folder nothing reads is worse than
  no file at all, because it looks like it worked. Pass -CreateFolder to make
  it deliberately.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Category,
    [Parameter(Mandatory = $true)][string]$Location,
    [int]$Max = 100,
    [int]$Target = 15,
    [int]$MinYears = 0,
    [switch]$NoApollo,
    [switch]$KeepSiteless,
    [string]$ExportFolder,
    [switch]$CreateFolder,
    [switch]$NoOpen,
    [switch]$Browser
)

$ErrorActionPreference = "Stop"

# Destination: -ExportFolder, else $env:LEAD_EXPORT_DIR, else the standing folder.
if (-not $ExportFolder) {
    $ExportFolder = if ($env:LEAD_EXPORT_DIR) {
        $env:LEAD_EXPORT_DIR
    } else {
        Join-Path $env:USERPROFILE "ClaudeAssistant\exports"
    }
}

if (-not (Test-Path -LiteralPath $ExportFolder)) {
    if ($CreateFolder) {
        New-Item -ItemType Directory -Path $ExportFolder -Force | Out-Null
        Write-Host "Created $ExportFolder" -ForegroundColor Yellow
    } else {
        Write-Host "Export folder not found: $ExportFolder" -ForegroundColor Red
        Write-Host ""
        Write-Host "Not creating it automatically - if this is the wrong path, the run would" -ForegroundColor Yellow
        Write-Host "look successful while writing where nothing reads. Check the real path:" -ForegroundColor Yellow
        Write-Host ""
        $guesses = @(
            (Join-Path $env:USERPROFILE "ClaudeAssistant\exports"),
            (Join-Path $env:USERPROFILE "ClaudeAssistant"),
            (Join-Path $env:USERPROFILE "OneDrive\ClaudeAssistant\exports"),
            "C:\ClaudeAssistant\exports"
        ) | Where-Object { Test-Path -LiteralPath $_ }
        if ($guesses) {
            Write-Host "  These exist - did you mean one of them?" -ForegroundColor Cyan
            $guesses | ForEach-Object { Write-Host "    $_" -ForegroundColor Cyan }
        } else {
            Write-Host "  None of the usual ClaudeAssistant paths exist on this machine." -ForegroundColor DarkGray
        }
        Write-Host ""
        Write-Host "Then either pass  -ExportFolder '<the real path>'" -ForegroundColor DarkGray
        Write-Host "or, if this path is right and just missing, re-run with -CreateFolder" -ForegroundColor DarkGray
        exit 2
    }
}

$stamp    = Get-Date -Format "yyyy-MM-dd-HHmm"
$catLabel = ($Category -join "+")
$out      = Join-Path $ExportFolder "$catLabel-$Location-$stamp.csv"
$report   = Join-Path $ExportFolder "$catLabel-$Location-$stamp.json"

$scraperArgs = @(
    "scraper.py",
    "--category", ($Category -join ","),
    "--location", $Location,
    "--max-results", $Max,
    "--output", $out,
    "--report", $report,
    "--column-map", (Join-Path $PSScriptRoot "lead-format.json"),
    "-v"
)
if ($MinYears -gt 0) { $scraperArgs += @("--min-years", $MinYears) }
if ($Target -gt 0)   { $scraperArgs += @("--target-rows", $Target) }
if ($Browser)        { $scraperArgs += "--browser" }

# BBB's profile pages 403, so `website` arrives blank and the website filter
# would drop everything. Apollo's free lookup fills it back in first -- which
# is why -KeepSiteless is refused without it rather than quietly passing.
if (-not $NoApollo) {
    if ($env:APOLLO_API_KEY) {
        $scraperArgs += "--apollo"
        if (-not $KeepSiteless) { $scraperArgs += "--require-website" }
    } else {
        Write-Host "APOLLO_API_KEY is not set - skipping the website lookup." -ForegroundColor Yellow
        Write-Host "  Websites will be blank, so no website filter is applied." -ForegroundColor DarkGray
        Write-Host '  Fix with:  setx APOLLO_API_KEY "<your key>"  (then reopen PowerShell)' -ForegroundColor DarkGray
    }
}

Write-Host "Pulling $catLabel in $Location (cap $Max)" -ForegroundColor Cyan
Write-Host "Export to: $out`n" -ForegroundColor DarkGray

python @scraperArgs
$code = $LASTEXITCODE

if ($code -eq 0 -and (Test-Path -LiteralPath $out)) {
    $rows = @(Import-Csv $out).Count
    Write-Host "`n$rows leads exported to $out" -ForegroundColor Green
    Write-Host "Next: python crm_check.py `"$out`"   (needs `$env:HUBSPOT_TOKEN)" -ForegroundColor DarkGray
    if (-not $NoOpen) { explorer $ExportFolder }
} else {
    Write-Host "`nNo file was written - see the messages above." -ForegroundColor Yellow
}
exit $code
