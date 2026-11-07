<#
.SYNOPSIS
  Starts the model services and the gateway locally (no Docker needed).

.EXAMPLE
  pwsh -File scripts/dev.ps1
  pwsh -File scripts/dev.ps1 -Stop
#>
param(
    [switch]$Stop,
    [int]$ApiPort = 8000,
    [int]$ModelAPort = 8101,
    [int]$ModelBPort = 8102
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root '.run'
$logDir = Join-Path $root 'reports\logs'
New-Item -ItemType Directory -Force -Path $runDir, $logDir | Out-Null

function Stop-LocalStack {
    Get-ChildItem -Path $runDir -Filter '*.pid' -ErrorAction SilentlyContinue | ForEach-Object {
        $pidFile = $_.FullName
        $processId = (Get-Content $pidFile -Raw).Trim()
        if ($processId -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $processId -Force
            Write-Host "stopped pid $processId ($($_.BaseName))"
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

if ($Stop) {
    Stop-LocalStack
    exit 0
}

Stop-LocalStack

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "virtualenv missing. Run: uv sync --extra dev" }

function Start-Component {
    param(
        [string]$Name,
        [string]$Module,
        [hashtable]$Env,
        [int]$Port
    )
    $stdout = Join-Path $logDir "$Name.out.log"
    $stderr = Join-Path $logDir "$Name.err.log"
    $envArgs = @{}
    foreach ($key in $Env.Keys) { $envArgs[$key] = $Env[$key] }
    $process = Start-Process -FilePath $python `
        -ArgumentList '-m', $Module `
        -WorkingDirectory $root `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru `
        -Environment $envArgs
    Set-Content -Path (Join-Path $runDir "$Name.pid") -Value $process.Id
    Write-Host "started $Name pid=$($process.Id) port=$Port logs=$logDir"
}

$common = @{ 'LOG_LEVEL' = 'INFO'; 'LOG_JSON' = 'true' }

Start-Component -Name 'model-a' -Module 'services.model_service' -Port $ModelAPort -Env @{
    'MODEL_PORT'   = "$ModelAPort"
    'MODEL_NAME'   = 'llm-fast'
    'MODEL_TTFT_MS' = '80'
    'MODEL_TOKENS_PER_SEC' = '120'
} + $common

Start-Component -Name 'model-b' -Module 'services.model_service' -Port $ModelBPort -Env @{
    'MODEL_PORT'   = "$ModelBPort"
    'MODEL_NAME'   = 'llm-lite'
    'MODEL_TTFT_MS' = '30'
    'MODEL_TOKENS_PER_SEC' = '220'
} + $common

Start-Component -Name 'gateway' -Module 'services.api' -Port $ApiPort -Env @{
    'API_PORT'     = "$ApiPort"
    'MODEL_A_URL'  = "http://127.0.0.1:$ModelAPort"
    'MODEL_B_URL'  = "http://127.0.0.1:$ModelBPort"
    'MAX_INFLIGHT' = '32'
    'MAX_QUEUE'    = '64'
    'RATE_LIMIT_RPM' = '6000'
    'RATE_LIMIT_TPM' = '600000'
    'RATE_LIMIT_BURST' = '60'
} + $common

$targets = @(
    "http://127.0.0.1:$ModelAPort/healthz",
    "http://127.0.0.1:$ModelBPort/healthz",
    "http://127.0.0.1:$ApiPort/healthz"
)

foreach ($target in $targets) {
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $target -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 250 }
    }
    $state = if ($ready) { 'ready' } else { 'NOT READY' }
    Write-Host "$target -> $state"
}

Write-Host ''
Write-Host "gateway: http://127.0.0.1:$ApiPort"
Write-Host "metrics: http://127.0.0.1:$ApiPort/metrics"
Write-Host "status:  http://127.0.0.1:$ApiPort/admin/status"

