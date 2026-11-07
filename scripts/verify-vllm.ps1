<#
.SYNOPSIS
  End-to-end check of the vLLM path: engine -> proxy -> gateway -> client.

.EXAMPLE
  pwsh -File scripts/verify-vllm.ps1
#>
param(
    [int]$BigPort = 8001,
    [int]$SmallPort = 8002,
    [int]$GatewayPort = 8080
)

$ErrorActionPreference = 'Stop'

function Step($message) { Write-Host "`n### $message" -ForegroundColor Cyan }

Step 'vLLM engines'
foreach ($port in $BigPort, $SmallPort) {
    try {
        $models = Invoke-RestMethod "http://127.0.0.1:$port/v1/models" -TimeoutSec 5
        Write-Host "port $port -> $($models.data.id -join ', ')"
    } catch {
        Write-Warning "port $port not answering: $($_.Exception.Message)"
    }
}

Step 'model service proxies'
foreach ($port in 8101, 8102) {
    try {
        $info = Invoke-RestMethod "http://127.0.0.1:$port/readyz" -TimeoutSec 5
        Write-Host "port $port -> backend=$($info.backend) status=$($info.status) vllm=$($info.vllm_reachable)"
    } catch {
        Write-Warning "port $port not ready: $($_.Exception.Message)"
    }
}

Step 'gateway: streaming request through the whole chain'
$body = @{
    messages = @(@{ role = 'user'; content = 'In one sentence, what does a load balancer do?' })
    max_tokens = 64
    stream = $true
} | ConvertTo-Json -Depth 5

$raw = curl.exe -s -N -X POST "http://127.0.0.1:$GatewayPort/v1/chat/completions" `
    -H "content-type: application/json" -H "x-tenant-id: verify" -d $body
$raw | Select-Object -First 3
$text = ($raw | Where-Object { $_ -like 'data: *' -and $_ -notlike '*[DONE]*' }) -join ''

Step 'gateway: non-streaming request'
$body2 = @{
    messages = @(@{ role = 'user'; content = 'Name three SLOs for an inference gateway.' })
    max_tokens = 96
    stream = $false
} | ConvertTo-Json -Depth 5
$resp = Invoke-WebRequest "http://127.0.0.1:$GatewayPort/v1/chat/completions" `
    -Method Post -Body $body2 -ContentType 'application/json' -TimeoutSec 120
Write-Host "status=$($resp.StatusCode) x-upstream=$($resp.Headers['x-upstream']) x-model=$($resp.Headers['x-model'])"
$parsed = $resp.Content | ConvertFrom-Json
Write-Host "content: $($parsed.choices[0].message.content)"
Write-Host "usage: $($parsed.usage | ConvertTo-Json -Compress)"

Step 'gateway: status'
Invoke-RestMethod "http://127.0.0.1:$GatewayPort/admin/status" |
    ConvertTo-Json -Depth 4 |
    Select-Object -First 40

