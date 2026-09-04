<#
.SYNOPSIS
  Show how a real BBB profile page presents the fields the screen needs.

.DESCRIPTION
  Detail pages load through the browser but every field parses as empty, which
  means the markup does not match what the parser expects. This prints the real
  markup so the parser can be written against it rather than guessed at.

  Defaults to a Houston roofing profile from the last test run.

.EXAMPLE
  .\inspect-detail.ps1
  .\inspect-detail.ps1 -Url "https://www.bbb.org/us/tx/houston/profile/..."
#>
param(
    [string]$Url = "https://www.bbb.org/us/tx/houston/profile/roofing-contractors/whitmans-contracting-roofing-0915-90045564"
)

$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

& python inspect_detail.py $Url 2>&1 | ForEach-Object { $_.ToString() }

Write-Host ""
Write-Host "Full page saved to detail-sample.html - keep it, it is the fixture." -ForegroundColor DarkGray
