<#
.SYNOPSIS
  Runs the load harness against a running gateway.

.EXAMPLE
  pwsh -File scripts/loadtest.ps1 -Duration 30 -Concurrency 16
#>
param(
    [string]$Url = 'http://127.0.0.1:8000',
    [int]$Duration = 30,
    [int]$Concurrency = 16,
    [int]$Ramp = 5,
    [int]$Warmup = 5,
    [switch]$Blocking,
    [string]$Out = 'reports/load-latest.json',
    [string]$ChaosUrl = '',
    [string]$ChaosMode = 'none',
    [double]$ChaosRate = 0.5,
    [double]$ChaosAt = 10
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

$arguments = @(
    '-m', 'load.harness',
    '--url', $Url,
    '--duration', $Duration,
    '--concurrency', $Concurrency,
    '--ramp', $Ramp,
    '--warmup', $Warmup,
    '--out', $Out
)
if ($Blocking) { $arguments += '--blocking' }
if ($ChaosMode -ne 'none') {
    $arguments += @('--chaos-url', $ChaosUrl, '--chaos-mode', $ChaosMode,
        '--chaos-rate', $ChaosRate, '--chaos-at', $ChaosAt)
}

& (Join-Path $root '.venv\Scripts\python.exe') @arguments
$code = $LASTEXITCODE
Pop-Location
exit $code

