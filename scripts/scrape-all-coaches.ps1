# Scrape staff directories for every school in the store, in batches.
#
# A single 1700-site run would take hours and write nothing until it finishes --
# the store is only saved at the end of a run. Batching means a crash, a reboot
# or a Ctrl+C costs you one batch, not the whole night.
#
# Usage:
#   .\scripts\scrape-all-coaches.ps1
#   .\scripts\scrape-all-coaches.ps1 -Seeds data\seeds\naia.txt -BatchSize 50
#   .\scripts\scrape-all-coaches.ps1 -StartAt 7      # resume after batch 6

param(
    [string]$Seeds = "data\seeds\all-schools.txt",
    [int]$BatchSize = 100,
    [int]$StartAt = 1,
    # Sites in flight at once. The 1.5s delay is per-host, so a higher number
    # spreads across different schools rather than hammering one.
    [int]$Concurrency = 0,
    # Download coach headshots as well. Roughly doubles the requests per site,
    # so it roughly doubles the wall clock. The photo URL is recorded either way.
    [switch]$SavePhotos,
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path $Seeds)) {
    Write-Host "No seed file at $Seeds. Generate one first:" -ForegroundColor Red
    Write-Host "  $Python -m scrapbot.cli seeds --out $Seeds"
    exit 1
}

# Drop comments and blanks so the batch numbers match real work.
$hosts = Get-Content $Seeds | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith("#") }
$total = $hosts.Count
$batches = [math]::Ceiling($total / $BatchSize)

$batchDir = Join-Path (Split-Path $Seeds) "batches"
New-Item -ItemType Directory -Force $batchDir | Out-Null

Write-Host "$total host(s) -> $batches batch(es) of $BatchSize" -ForegroundColor Cyan

for ($i = $StartAt; $i -le $batches; $i++) {
    $slice = $hosts | Select-Object -Skip (($i - 1) * $BatchSize) -First $BatchSize
    $file = Join-Path $batchDir ("batch-{0:d3}.txt" -f $i)
    # Out-File -Encoding utf8 writes a BOM on PowerShell 5.1, and the BOM sticks
    # to the first hostname in the file -- one dead site per batch. WriteAllLines
    # gives UTF-8 without one.
    [System.IO.File]::WriteAllLines($file, [string[]]$slice, (New-Object System.Text.UTF8Encoding $false))

    Write-Host ""
    Write-Host "=== batch $i / $batches  ($($slice.Count) sites) ===" -ForegroundColor Cyan
    $started = Get-Date

    $extra = @()
    if ($Concurrency -gt 0) { $extra += @("--concurrency", $Concurrency) }
    if ($SavePhotos) { $extra += "--save-photos" }
    & $Python -m scrapbot.cli run coaches --seeds $file @extra

    $mins = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
    if ($LASTEXITCODE -ne 0) {
        # Keep going: one bad batch should not stop the other 1600 schools.
        Write-Host "batch $i exited $LASTEXITCODE after $mins min -- continuing" -ForegroundColor Yellow
    } else {
        Write-Host "batch $i done in $mins min" -ForegroundColor Green
    }
    Write-Host "resume from here with: -StartAt $($i + 1)"
}

Write-Host ""
Write-Host "All batches finished. Review:" -ForegroundColor Cyan
Write-Host "  $Python -m scrapbot.cli stats --contacts"
Write-Host "  dashboard -> Runs tab, retry anything that failed"
