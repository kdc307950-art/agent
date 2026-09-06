[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$compose = @("compose", "-f", (Join-Path $root "infra/compose.demo.yml"))

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running. Start it and wait for Running before retrying."
}
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose is unavailable. Update Docker Desktop and retry."
}

Set-Location $root
& docker @compose exec -T agent python drill_fake_fmg.py
exit $LASTEXITCODE
