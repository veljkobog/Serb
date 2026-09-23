<#
.SYNOPSIS
  Solve the Cloudflare challenge once, by hand, and keep the clearance.

.DESCRIPTION
  BBB serves profile pages behind a Cloudflare challenge. Headless Chromium
  does not get through it. A visible browser, driven by a person, does -- and
  the clearance cookie is stored in the browser profile directory, which later
  headless runs reuse.

  This opens a real window at one profile page and waits. If a challenge
  appears, solve it (usually just a checkbox, sometimes nothing at all). Once
  you can see the business page, press Enter here.

  Clearance is not permanent -- expect to redo this every week or two, and
  whenever the daily run reports profile pages being challenged again.

.EXAMPLE
  .\warm-profile.ps1
#>
param(
    [string]$Url = "https://www.bbb.org/us/tx/houston/profile/roofing-contractors/whitmans-contracting-roofing-0915-90045564",
    [string]$ProfileDir = ".bbb-browser-profile"
)

$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "Opening a real browser window at a BBB profile page." -ForegroundColor Cyan
Write-Host "If Cloudflare asks you to verify, do it. When you can see the" -ForegroundColor Cyan
Write-Host "business profile, come back here and press Enter.`n" -ForegroundColor Cyan

& python warm_profile.py --url $Url --profile-dir $ProfileDir 2>&1 |
    ForEach-Object { $_.ToString() }
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "Clearance saved to $ProfileDir" -ForegroundColor Green
    Write-Host "Now check it actually worked:  .\test-browser.ps1" -ForegroundColor DarkGray
} else {
    Write-Host "Did not get a clear profile page. Paste the output above." -ForegroundColor Yellow
}
exit $code
