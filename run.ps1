# Start the whole app - API, vision-service and frontend - in Docker.
# (The database is hosted Supabase, reached over HTTPS - nothing local.)
#
#   .\run.ps1          start everything (rebuilds if code changed)
#   .\run.ps1 stop     stop everything
#   .\run.ps1 logs     follow the logs
#
# Everything runs detached, so this returns to your prompt when it's ready.

param(
    [ValidateSet("start", "stop", "logs")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$ApiUrl = "http://localhost:8000"
$AppUrl = "http://localhost:8080"

if ($Action -eq "stop") {
    Write-Host "==> Stopping..."
    docker compose down
    Write-Host "Done."
    exit 0
}

if ($Action -eq "logs") {
    docker compose logs -f
    exit 0
}

docker info *> $null
if (-not $?) {
    Write-Host "ERROR: Docker isn't running. Start Docker Desktop, then re-run this." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path backend/.env)) {
    Copy-Item backend/.env.example backend/.env
    Write-Host "==> Created backend/.env from .env.example"
}

Write-Host "==> Building and starting containers..."
docker compose up -d --build

Write-Host "==> Waiting for the API..."
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "$ApiUrl/health" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        Start-Sleep -Seconds 1
    }
}

if (-not $ready) {
    Write-Host "ERROR: the API never became healthy. Check: .\run.ps1 logs" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  App        $AppUrl"
Write-Host "  API docs   $ApiUrl/docs"
Write-Host ""
Write-Host "  No account yet?  $AppUrl/register.html"
Write-Host "  Need a funded test account?"
Write-Host "      docker compose exec backend python -m scripts.seed_dev_user"
Write-Host ""
Write-Host "  .\run.ps1 logs   follow logs"
Write-Host "  .\run.ps1 stop   stop everything"
Write-Host ""
