#!/usr/bin/env bash
# Bootstraps a dedicated WSL2 distro for vLLM.
#
# Why a separate distro: vLLM imports tens of thousands of small files at
# startup. On a DrvFs mount (/mnt/d) every one of those is a 9p RPC, and the
# import alone takes tens of minutes. A dedicated distro whose ext4 lives on
# D: keeps the install off the Windows system drive *and* on a real Linux
# filesystem.
#
# Usage (from WSL, inside the new distro):
#   bash /mnt/d/AI\ infra/scripts/setup-vllm-distro.sh
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
VENV="${VENV:-/opt/vllm-venv}"

echo "==> distro"
head -2 /etc/os-release
echo "==> gpu"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || echo "no nvidia-smi"

echo "==> apt prerequisites"
apt-get update -qq
# build-essential is not optional: Triton's JIT needs a C compiler at runtime
# to build the kernels it generates for the GPU.
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl python3 python3-venv build-essential
python3 --version

echo "==> uv"
export UV_INSTALL_DIR=/usr/local/bin
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv.sh
sh /tmp/uv.sh >/dev/null
export PATH=/usr/local/bin:$PATH
uv --version

echo "==> venv on ext4: $VENV"
if [ ! -x "$VENV/bin/python" ]; then
  uv venv --python 3.12 "$VENV"
fi

echo "==> installing vllm (torch + CUDA wheels, ~8GB, all on D:)"
# --system-certs makes uv use the distro CA store. Without it uv hits
# "CaUsedAsEndEntity" on networks that sit behind a TLS-inspecting proxy.
time uv pip install --system-certs --python "$VENV/bin/python" vllm

echo "==> verifying"
"$VENV/bin/python" - <<'PY'
import time

start = time.time()
import torch
print(f"torch {torch.__version__} cuda={torch.version.cuda} "
      f"available={torch.cuda.is_available()} ({time.time() - start:.1f}s to import)")
import vllm
print("vllm", vllm.__version__)
if torch.cuda.is_available():
    print("gpu:", torch.cuda.get_device_name(0))
PY

echo "==> ready. start with:"
echo "    $VENV/bin/vllm serve <model-path> --served-model-name big-model \\"
echo "      --host 0.0.0.0 --port 8001 --max-model-len 4096 \\"
echo "      --gpu-memory-utilization 0.60 --max-num-seqs 48"

