<#
.SYNOPSIS
  Runs the reference load-test scenarios from inside a Linux container.

  Running the harness on Windows adds ~40ms of asyncio socket overhead per
  request (curl to the same endpoint takes 3ms). Measuring from Linux keeps
  the numbers honest.

.EXAMPLE
  pwsh -File scripts/scenarios-docker.ps1
  pwsh -File scripts/scenarios-docker.ps1 -Only baseline
#>
param(
    [string]$Only = '',
    [int]$Duration = 30,
    [int]$Concurrency = 24,
    [string]$EnvFile = 'deploy/docker.env'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

$composeArgs = @('compose', '--env-file', $EnvFile, '-f', 'deploy/docker-compose.yml')

function Invoke-Scenario {
    param([string]$Name, [string[]]$Extra, [int]$Seconds)
    Write-Host ''
    Write-Host "### scenario: $Name" -ForegroundColor Cyan
    $args = @(
        '--profile', 'loadtest', 'run', '--rm', 'loadtest',
        '--url', 'http://gateway:8000',
        '--duration', $Seconds, '--out', "/app/reports/scenario-$Name.json"
    ) + $Extra
    & docker @composeArgs @args
    if ($LASTEXITCODE -ne 0) { Write-Warning "scenario $Name exited with $LASTEXITCODE" }
}

$scenarios = [ordered]@{
    'baseline' = @('--concurrency', $Concurrency, '--ramp', '6', '--warmup', '5',
        '--rate', '24',
        '--unique-prompts', '2000', '--max-tokens', '96', '--prompt-tokens', '180')
    'cache' = @('--concurrency', $Concurrency, '--ramp', '6', '--warmup', '5',
        '--rate', '24',
        '--unique-prompts', '8', '--max-tokens', '96', '--prompt-tokens', '180')
    'cache-saturation' = @('--concurrency', 64, '--ramp', '6', '--warmup', '5',
        '--rate', '400',
        '--unique-prompts', '8', '--max-tokens', '96', '--prompt-tokens', '180')
    'chaos-flaky' = @('--concurrency', $Concurrency, '--ramp', '6', '--warmup', '5',
        '--rate', '24',
        '--unique-prompts', '2000', '--max-tokens', '96', '--prompt-tokens', '180',
        '--chaos-url', 'http://model-a:8101', '--chaos-mode', 'http_500',
        '--chaos-rate', '0.5', '--chaos-at', '10', '--chaos-duration', '12')
    'chaos-down' = @('--concurrency', $Concurrency, '--ramp', '6', '--warmup', '5',
        '--rate', '24',
        '--unique-prompts', '2000', '--max-tokens', '96', '--prompt-tokens', '180',
        '--chaos-url', 'http://model-a:8101', '--chaos-mode', 'http_500',
        '--chaos-rate', '1.0', '--chaos-at', '8', '--chaos-duration', '14')
    'overload' = @('--concurrency', '128', '--ramp', '4', '--warmup', '6',
        '--unique-prompts', '20000', '--max-tokens', '96', '--prompt-tokens', '180')
}

foreach ($entry in $scenarios.GetEnumerator()) {
    if ($Only -and $Only -ne $entry.Key) { continue }
    $seconds = if ($entry.Key -like 'cache*') { 20 } else { $Duration }
    Invoke-Scenario -Name $entry.Key -Extra $entry.Value -Seconds $seconds
    if (-not $Only) {
        # Let injected faults expire and the breaker close before the next run.
        Start-Sleep -Seconds 15
    }
}

Write-Host ''
Write-Host 'reports written to reports/scenario-*.json and .md'
Pop-Location

