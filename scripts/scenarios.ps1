<#
.SYNOPSIS
  Runs the four reference load-test scenarios and writes reports.

.EXAMPLE
  pwsh -File scripts/scenarios.ps1
  pwsh -File scripts/scenarios.ps1 -Only chaos
#>
param(
    [string]$Url = 'http://127.0.0.1:8000',
    [string]$ChaosUrl = 'http://127.0.0.1:8101',
    [string]$Only = '',
    [int]$Duration = 30
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

function Invoke-Scenario {
    param(
        [string]$Name,
        [string[]]$Extra,
        [int]$Seconds
    )
    $out = "reports/scenario-$Name.json"
    Write-Host ''
    Write-Host "### scenario: $Name" -ForegroundColor Cyan
    & $python -m load.harness --url $Url --duration $Seconds --out $out @Extra
}

$scenarios = [ordered]@{
    'baseline' = @(
        '--concurrency', '24', '--ramp', '6', '--warmup', '5',
        '--unique-prompts', '2000', '--max-tokens', '96', '--prompt-tokens', '180'
    )
    'cache'    = @(
        '--concurrency', '24', '--ramp', '6', '--warmup', '5',
        '--unique-prompts', '8', '--max-tokens', '96', '--prompt-tokens', '180'
    )
    'chaos-flaky' = @(
        '--concurrency', '24', '--ramp', '6', '--warmup', '5',
        '--unique-prompts', '2000', '--max-tokens', '96', '--prompt-tokens', '180',
        '--chaos-url', $ChaosUrl, '--chaos-mode', 'http_500',
        '--chaos-rate', '0.5', '--chaos-at', '10', '--chaos-duration', '12'
    )
    'chaos-down' = @(
        '--concurrency', '24', '--ramp', '6', '--warmup', '5',
        '--unique-prompts', '2000', '--max-tokens', '96', '--prompt-tokens', '180',
        '--chaos-url', $ChaosUrl, '--chaos-mode', 'http_500',
        '--chaos-rate', '1.0', '--chaos-at', '8', '--chaos-duration', '14'
    )
    'overload' = @(
        '--concurrency', '96', '--ramp', '4', '--warmup', '5',
        '--unique-prompts', '4000', '--max-tokens', '96', '--prompt-tokens', '180'
    )
}

foreach ($entry in $scenarios.GetEnumerator()) {
    if ($Only -and $Only -ne $entry.Key) { continue }
    $seconds = if ($entry.Key -eq 'cache') { 20 } else { $Duration }
    Invoke-Scenario -Name $entry.Key -Extra $entry.Value -Seconds $seconds
    if (-not $Only -and $entry.Key -ne 'overload') {
        # Let injected faults expire and the breaker close before the next run.
        Start-Sleep -Seconds 15
    }
}

Write-Host ''
Write-Host 'reports written to reports/scenario-*.json and .md'

