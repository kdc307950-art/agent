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
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = @(& docker @compose @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if ($output.Count -gt 0) {
        $output | ForEach-Object { Write-Host $_ }
    }
    if ($exitCode -ne 0) {
        $details = ($output | Out-String)
        if ($details -match "registry-1\.docker\.io|failed to resolve source metadata|no HTTPS proxy|proxyconnect tcp|failed to fetch anonymous token|dial tcp .*:443") {
            throw "Docker Compose failed while reaching a container registry. Configure Docker Desktop HTTP/HTTPS proxy or preload the required base images, then retry."
        }
        throw "Docker Compose command failed with exit code $exitCode."
    }
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

function Test-LocalImage {
    param([Parameter(Mandatory = $true)][string]$Image)
    & docker image inspect $Image *> $null
    return $LASTEXITCODE -eq 0
}

function Use-CachedBuildImages {
    $fallbacks = @(
        @{
            Variable = "DEMO_UV_IMAGE"
            Preferred = "ghcr.io/astral-sh/uv:0.11"
            Cached = "ghcr.nju.edu.cn/astral-sh/uv:0.11"
            Label = "uv"
        },
        @{
            Variable = "DEMO_NODE_IMAGE"
            Preferred = "node:22-alpine"
            Cached = "docker.1ms.run/library/node:20-alpine"
            Label = "Node.js"
        },
        @{
            Variable = "DEMO_NGINX_IMAGE"
            Preferred = "nginx:1.27-alpine"
            Cached = "docker.1ms.run/library/nginx:1.27-alpine"
            Label = "Nginx"
        }
    )

    foreach ($item in $fallbacks) {
        $current = [Environment]::GetEnvironmentVariable($item.Variable, "Process")
        if (-not [string]::IsNullOrWhiteSpace($current)) { continue }
        if (Test-LocalImage $item.Preferred) { continue }
        if (Test-LocalImage $item.Cached) {
            [Environment]::SetEnvironmentVariable($item.Variable, $item.Cached, "Process")
            Write-Host "Using cached $($item.Label) image: $($item.Cached)"
        }
    }

    # Docker Desktop may inject a stale socks5://127.0.0.1 proxy from the
    # user's Docker config. Empty values make the demo build use direct access;
    # enterprise users can set DEMO_*_PROXY explicitly before running this file.
    foreach ($name in @("DEMO_HTTP_PROXY", "DEMO_HTTPS_PROXY", "DEMO_ALL_PROXY")) {
        if ($null -eq [Environment]::GetEnvironmentVariable($name, "Process")) {
            [Environment]::SetEnvironmentVariable($name, "", "Process")
        }
    }
}

function Wait-DemoReady {
    $deadline = (Get-Date).AddSeconds(120)
    do {
        try {
            # 8000 is the Nginx public port; /api/* is proxied to agent.
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/readyz" -TimeoutSec 3
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
Use-CachedBuildImages
if ($Down) {
    Invoke-Compose -Arguments @("down")
    Write-Host "Demo environment stopped."
    exit 0
}

if ($SkipBuild) {
    Invoke-Compose -Arguments @("up", "-d")
} else {
    Invoke-Compose -Arguments @("up", "-d", "--build")
}
Wait-DemoReady

Write-Host ""
Write-Host "Demo environment ready: http://127.0.0.1:8000"
Write-Host "Open the workbench and choose a fixed demo persona to sign in."
Write-Host ""
Write-Host "Fake FMG runs only on the internal Compose network. It is a local protocol fixture, not a real FMG."

if (-not $SkipDrill) {
    & (Join-Path $PSScriptRoot "drill-fmg.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
