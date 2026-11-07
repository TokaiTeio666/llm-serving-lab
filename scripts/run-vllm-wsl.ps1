<#
.SYNOPSIS
  Runs the whole stack against a real vLLM server hosted in WSL2.

  Topology:
    load client -> gateway (Docker) -> model-a proxy (Docker) -> vLLM (WSL2, GPU)
                                 -> model-b proxy (Docker) -> vLLM (WSL2, GPU)

  The model service keeps fault injection and admission control in front of
  vLLM, so the gateway's breaker / fallback paths stay testable against a
  real engine.

.EXAMPLE
  pwsh -File scripts/run-vllm-wsl.ps1 -Start
  pwsh -File scripts/run-vllm-wsl.ps1 -Scenario baseline
  pwsh -File scripts/run-vllm-wsl.ps1 -Stop
#>
param(
    [switch]$Start,
    [switch]$Stop,
    [string]$Scenario = '',
    [string]$Distro = 'vllm',
    [string]$EnvFile = 'deploy/docker.env.wsl',
    [string]$BigModel = '/mnt/d/ai-infra/models/Qwen2.5-1.5B-Instruct',
    [string]$SmallModel = '/mnt/d/ai-infra/models/Qwen2.5-0.5B-Instruct',
    [int]$BigPort = 8001,
    [int]$SmallPort = 8002,
    [int]$Duration = 30,
    [int]$Concurrency = 24
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root '.run'
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }

function Stop-Vllm {
    wsl -d $Distro -- bash -lc "pkill -f 'vllm serve' 2>/dev/null; exit 0" | Out-Null
    Write-Host "vllm stopped"
}

if ($Stop) {
    Stop-Vllm
    exit 0
}

function Wait-Http {
    param([string]$Url, [int]$Seconds = 300, [string]$Label = 'service')
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                Write-Host "$Label ready at $Url" -ForegroundColor Green
                return $true
            }
        } catch { }
        Start-Sleep -Seconds 3
    }
    Write-Warning "$Label did not become ready within $Seconds s"
    return $false
}

if ($Start) {
    Stop-Vllm
    Write-Step "starting vLLM (big) on port $BigPort"
    $bigCmd = "wsl -d $Distro -- env " +
              "VLLM_MODEL=$BigModel VLLM_PORT=$BigPort VLLM_SERVED_NAME=big-model " +
              "VLLM_GPU_MEMORY_UTILIZATION=${env:VLLM_BIG_UTIL:-0.60} " +
              "VLLM_MAX_NUM_SEQS=${env:VLLM_MAX_NUM_SEQS:-48} " +
              "VLLM_MAX_MODEL_LEN=${env:VLLM_BIG_MAX_LEN:-4096} " +
              "bash '/mnt/d/AI infra/scripts/start-vllm-wsl.sh'"
    Start-Process pwsh -ArgumentList '-NoProfile', '-Command', $bigCmd `
        -RedirectStandardOutput (Join-Path $runDir 'vllm-big.out.log') `
        -RedirectStandardError (Join-Path $runDir 'vllm-big.err.log') `
        -WindowStyle Hidden

    $smallCmd = "wsl -d $Distro -- bash '/mnt/d/AI infra/scripts/start-vllm-wsl.sh' " +
                "VLLM_MODEL=$SmallModel VLLM_PORT=$SmallPort VLLM_SERVED_NAME=small-model " +
                "VLLM_GPU_MEMORY_UTILIZATION=${env:VLLM_SMALL_UTIL:-0.22} " +
                "VLLM_MAX_NUM_SEQS=${env:VLLM_MAX_NUM_SEQS:-16} " +
                "VLLM_MAX_MODEL_LEN=${env:VLLM_SMALL_MAX_LEN:-2048}"
    $smallCmd = "wsl -d $Distro -- env " +
                "VLLM_MODEL=$SmallModel VLLM_PORT=$SmallPort VLLM_SERVED_NAME=small-model " +
                "VLLM_GPU_MEMORY_UTILIZATION=${env:VLLM_SMALL_UTIL:-0.22} " +
                "VLLM_MAX_NUM_SEQS=${env:VLLM_MAX_NUM_SEQS:-16} " +
                "VLLM_MAX_MODEL_LEN=${env:VLLM_SMALL_MAX_LEN:-2048} " +
                "bash '/mnt/d/AI infra/scripts/start-vllm-wsl.sh'"
    Write-Step "starting vLLM (small, degraded fallback) on port $SmallPort"
    Start-Process pwsh -ArgumentList '-NoProfile', '-Command', $smallCmd `
        -RedirectStandardOutput (Join-Path $runDir 'vllm-small.out.log') `
        -RedirectStandardError (Join-Path $runDir 'vllm-small.err.log') `
        -WindowStyle Hidden

    Wait-Http -Url "http://127.0.0.1:$BigPort/health" -Seconds 600 -Label 'vllm (big)'
    Wait-Http -Url "http://127.0.0.1:$SmallPort/health" -Seconds 600 -Label 'vllm (small)'
}

Write-Step "pointing the model service at vLLM"
$env:MODEL_BACKEND = 'vllm'
$env:VLLM_A_URL = "http://host.docker.internal:$BigPort"
$env:VLLM_B_URL = "http://host.docker.internal:$SmallPort"
$env:VLLM_A_MODEL = 'big-model'
$env:VLLM_B_MODEL = 'small-model'
$env:VLLM_TIMEOUT_S = '300'

Push-Location $root
docker compose --env-file $EnvFile -f deploy/docker-compose.yml up -d --force-recreate model-a model-b 2>&1 | Select-Object -Last 3
Pop-Location

Start-Sleep -Seconds 8
foreach ($p in 8101, 8102) {
    try {
        $info = Invoke-RestMethod "http://127.0.0.1:$p/readyz" -TimeoutSec 5
        Write-Host "model on $p -> backend=$($info.backend) status=$($info.status) vllm_reachable=$($info.vllm_reachable)"
    } catch {
        Write-Warning "model on $p not ready: $($_.Exception.Message)"
    }
}

# The gateway caches exact request payloads. Switching the upstream backend
# changes the answer for the same payload, so stale entries have to go.
try {
    Invoke-RestMethod "http://127.0.0.1:$GatewayPort/admin/cache" -Method Delete -TimeoutSec 5 |
        Out-Null
    Write-Host "gateway cache flushed"
} catch {
    Write-Warning "could not flush the gateway cache: $($_.Exception.Message)"
}

if ($Scenario) {
    Write-Step "running scenario: $Scenario"
    $extra = @()
    if ($Scenario -like 'chaos*') {
        $extra = @('--chaos-url', "http://host.docker.internal:$BigPort", '--chaos-mode', 'http_500',
                   '--chaos-rate', '0.5', '--chaos-at', '10', '--chaos-duration', '12')
    }
    Push-Location $root
    docker compose --env-file $EnvFile -f deploy/docker-compose.yml `
        --profile loadtest run --rm loadtest `
        --url http://gateway:8000 --duration $Duration --concurrency $Concurrency `
        --ramp 8 --warmup 10 --rate 12 --unique-prompts 500 --max-tokens 64 `
        --out "/app/reports/scenario-vllm-$Scenario.json" @extra
    Pop-Location
}

