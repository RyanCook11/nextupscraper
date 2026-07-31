# Prepare a folder per blocked site, ready for pages you save from your browser.
#
# Sites that refuse automated requests still serve their staff directory to a
# person. Save the page (Ctrl+S, "Webpage, HTML Only") into its folder and run:
#
#   .venv\Scripts\python.exe -m scrapbot.cli run coaches --manual-dir data\manual
#
# Usage:
#   .\scripts\manual-folders.ps1                  # every unresolved blocked host
#   .\scripts\manual-folders.ps1 -Top 25          # just the first 25
#   .\scripts\manual-folders.ps1 -Open            # also open each one in the browser
#
# -Seeds narrows it to a list you already have, instead of every blocked host in
# the store. Any seed file works, so a subset -- "the blocked schools from one
# spreadsheet" -- can be scaffolded without touching the rest:
#
#   .\scripts\manual-folders.ps1 -Seeds data\seeds\blocked-from-list.txt

param(
    [string]$Root = "data\manual",
    [string]$Seeds,
    [int]$Top = 0,
    [switch]$Open,
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if ($Seeds) {
    if (-not (Test-Path $Seeds)) { throw "seed file not found: $Seeds" }
    $seedFile = $Seeds
} else {
    $seedFile = Join-Path $env:TEMP "scrapbot-blocked.txt"
    & $Python -m scrapbot.cli seeds --from-failures blocked --out $seedFile
    if (-not (Test-Path $seedFile)) { Write-Host "nothing blocked" -ForegroundColor Green; exit 0 }
}

# The comment after a host is the school it belongs to, which is the one thing
# that makes a folder of 569 hostnames navigable later. Keep it for _WHERE.txt.
$entries = Get-Content $seedFile |
    Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith("#") } |
    ForEach-Object {
        $parts = $_ -split "\s+#", 2
        [pscustomobject]@{
            Host  = $parts[0].Trim()
            Label = if ($parts.Count -gt 1) { $parts[1].Trim() } else { "" }
        }
    }
if ($Top -gt 0) { $entries = $entries | Select-Object -First $Top }

New-Item -ItemType Directory -Force $Root | Out-Null
$made = 0
foreach ($e in $entries) {
    $h = $e.Host
    $dir = Join-Path $Root $h
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null; $made++ }
    # A note in each folder so it is obvious later what belongs there.
    $readme = Join-Path $dir "_WHERE.txt"
    if (-not (Test-Path $readme)) {
        $who = if ($e.Label) { "$($e.Label)`r`n" } else { "" }
        "${who}Save the staff-directory page for $h here as .html`r`nTry: https://$h/staff-directory" |
            Out-File -FilePath $readme -Encoding utf8
    }
    if ($Open) { Start-Process "https://$h/staff-directory" }
}

Write-Host ""
Write-Host "$made folder(s) created under $Root (of $($entries.Count) blocked host(s))" -ForegroundColor Cyan
Write-Host "Save each staff-directory page as HTML into its folder, then run:"
Write-Host "  $Python -m scrapbot.cli run coaches --manual-dir $Root"
