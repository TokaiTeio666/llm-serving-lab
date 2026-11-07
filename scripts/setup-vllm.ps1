<#
.SYNOPSIS
  One-shot setup for running vLLM on WSL2 with an NVIDIA GPU.

  Creates a dedicated WSL distro whose ext4 lives on D: (so the ~9GB venv
  does not touch the Windows system drive and imports stay fast), installs
  vLLM in it, and downloads the models.

.EXAMPLE
  pwsh -File scripts/setup-vllm.ps1
  pwsh -File scripts/setup-vllm.ps1 -Distro vllm -BigModel Qwen/Qwen2.5-1.5B-Instruct
#>
param(
    [string]$Distro = 'vllm',
    [string]$InstallPath = 'D:\wsl\vllm',
    [string]$BigModel = 'Qwen/Qwen2.5-1.5B-Instruct',
    [string]$SmallModel = 'Qwen/Qwen2.5-0.5B-Instruct',
    [int]$Connections = 12
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$work = Join-Path $root '.run'
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

$rootfs = Join-Path (Split-Path $InstallPath -Parent) 'ubuntu-base.tar.gz'
$rootfsUrl = 'https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cdimage/ubuntu-base/releases/22.04/release/ubuntu-base-22.04-base-amd64.tar.gz'

$existing = (wsl -l -v 2>&1 | Out-String)
if ($existing -match [regex]::Escape($Distro)) {
    Step "distro '$Distro' already exists, skipping import"
} else {
    if (-not (Test-Path $rootfs)) {
        Step "downloading ubuntu-base rootfs"
        Invoke-WebRequest $rootfsUrl -OutFile $rootfs -UseBasicParsing -TimeoutSec 600
    }
    Step "importing distro '$Distro' -> $InstallPath"
    wsl --import $Distro $InstallPath $rootfs --version 2 | Out-Null
}

Step "installing vLLM inside '$Distro' (this takes a while)"
wsl -d $Distro -- bash "/mnt/d/AI infra/scripts/setup-vllm-distro.sh" 2>&1 |
    Tee-Object -FilePath (Join-Path $work 'vllm-install.log') |
    Select-Object -Last 8

Step "downloading models"
foreach ($model in @($BigModel, $SmallModel)) {
    $name = Split-Path $model -Leaf
    wsl -d $Distro -- bash "/mnt/d/AI infra/scripts/download-model.sh" $model `
        "/mnt/d/ai-infra/models/$name" 'https://modelscope.cn/models' $Connections 2>&1 |
        Select-Object -Last 3
}

Step "done. next: pwsh -File scripts/run-vllm-wsl.ps1 -Start"

