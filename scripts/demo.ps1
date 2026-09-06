[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$SkipDrill,
    [switch]$Down
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$compose = @("compose", "-f", (Join-Path $root "infra/compose.demo.yml"))

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @compose @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose command failed." }
}

function Assert-DockerReady {
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
}

function Wait-DemoReady {
    $deadline = (Get-Date).AddSeconds(120)
    do {
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/readyz" -TimeoutSec 3
            if ($response.status -eq "ready") { return }
        } catch {
            # Containers are still starting; keep probing until the deadline.
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Demo service was not ready within 120 seconds. Run 'docker compose -f infra/compose.demo.yml logs' for details."
}

Set-Location $root
Assert-DockerReady
if ($Down) {
    Invoke-Compose down
    Write-Host "Demo environment stopped."
    exit 0
}

if ($SkipBuild) {
    Invoke-Compose up -d
} else {
    Invoke-Compose up -d --build
}
Wait-DemoReady

$customerToken = (& docker @compose exec -T agent python -m backend.issue_dev_token demo customer-1 --role helpdesk-customer).Trim()
$agentToken = (& docker @compose exec -T agent python -m backend.issue_dev_token demo agent-1 --role helpdesk-agent).Trim()
$approverToken = (& docker @compose exec -T agent python -m backend.issue_dev_token demo approver-1 --role helpdesk-approver).Trim()
if ($LASTEXITCODE -ne 0) { throw "Development token generation failed." }

Write-Host ""
Write-Host "Demo environment ready: http://127.0.0.1:8000"
Write-Host "Customer token: $customerToken"
Write-Host "Agent token: $agentToken"
Write-Host "Approver token: $approverToken"
Write-Host ""
Write-Host "Fake FMG runs only on the internal Compose network. It is a local protocol fixture, not a real FMG."

if (-not $SkipDrill) {
    & (Join-Path $PSScriptRoot "drill-fmg.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
