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

param(
    [string]$Root = "data\manual",
    [int]$Top = 0,
    [switch]$Open,
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$seedFile = Join-Path $env:TEMP "scrapbot-blocked.txt"

& $Python -m scrapbot.cli seeds --from-failures blocked --out $seedFile
if (-not (Test-Path $seedFile)) { Write-Host "nothing blocked" -ForegroundColor Green; exit 0 }

$hosts = Get-Content $seedFile |
    Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith("#") } |
    ForEach-Object { ($_ -split "\s+#")[0].Trim() }
if ($Top -gt 0) { $hosts = $hosts | Select-Object -First $Top }

New-Item -ItemType Directory -Force $Root | Out-Null
$made = 0
foreach ($h in $hosts) {
    $dir = Join-Path $Root $h
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null; $made++ }
    # A note in each folder so it is obvious later what belongs there.
    $readme = Join-Path $dir "_WHERE.txt"
    if (-not (Test-Path $readme)) {
        "Save the staff-directory page for $h here as .html`r`nTry: https://$h/staff-directory" |
            Out-File -FilePath $readme -Encoding utf8
    }
    if ($Open) { Start-Process "https://$h/staff-directory" }
}

Write-Host ""
Write-Host "$made folder(s) created under $Root (of $($hosts.Count) blocked host(s))" -ForegroundColor Cyan
Write-Host "Save each staff-directory page as HTML into its folder, then run:"
Write-Host "  $Python -m scrapbot.cli run coaches --manual-dir $Root"
