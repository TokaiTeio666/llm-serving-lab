<#
.SYNOPSIS
  Toggles fault injection on a model service.

.EXAMPLE
  pwsh -File scripts/chaos.ps1 -Target http://127.0.0.1:8101 -Mode http_500 -Rate 0.4
  pwsh -File scripts/chaos.ps1 -Target http://127.0.0.1:8101 -Clear
#>
param(
    [string]$Target = 'http://127.0.0.1:8101',
    [string]$Mode = 'none',
    [double]$Rate = 1.0,
    [int]$Duration = 30,
    [switch]$Clear
)

$ErrorActionPreference = 'Stop'

if ($Clear) {
    Invoke-RestMethod -Uri "$Target/admin/faults" -Method Delete | ConvertTo-Json
    exit 0
}

$body = @{ mode = $Mode; rate = $Rate; duration_s = $Duration } | ConvertTo-Json
Invoke-RestMethod -Uri "$Target/admin/faults" -Method Post -Body $body `
    -ContentType 'application/json' | ConvertTo-Json

