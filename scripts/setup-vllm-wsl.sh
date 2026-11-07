#!/usr/bin/env bash
# Installs vLLM into a WSL2 Ubuntu venv and starts the OpenAI-compatible server.
#
# Everything lands on /mnt/d so it does not compete for space on the Windows
# system drive. Weights come from hf-mirror.com by default because
# huggingface.co is unreachable from this network.
#
# Usage (from WSL):
#   bash /mnt/d/AI\ infra/scripts/setup-vllm-wsl.sh [MODEL] [PORT]
set -euo pipefail

MODEL="${1:-Qwen/Qwen2.5-1.5B-Instruct}"
PORT="${2:-8001}"
SERVED_NAME="${SERVED_NAME:-$(basename "$MODEL" | tr 'A-Z' 'a-z')}"

WORKDIR="${WORKDIR:-/mnt/d/ai-infra}"
VENV="$WORKDIR/.vllm-venv"
export HF_HOME="${HF_HOME:-$WORKDIR/.hf-cache}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
# Keep the ~10GB wheel cache off the Windows system drive.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$WORKDIR/.uv-cache}"
export UV_LINK_MODE=copy
export PATH="$HOME/.local/bin:$PATH"

echo "model=$MODEL port=$PORT venv=$VENV"
echo "HF_ENDPOINT=$HF_ENDPOINT HF_HOME=$HF_HOME"

if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv"
  export UV_INSTALL_DIR="$HOME/.local/bin"
  mkdir -p "$UV_INSTALL_DIR"
  curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
  sh /tmp/uv-install.sh
fi

mkdir -p "$WORKDIR"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating venv (python 3.12)"
  uv venv --python 3.12 "$VENV"
fi

echo "==> installing vllm (this pulls torch + CUDA wheels, ~8GB)"
uv pip install --python "$VENV/bin/python" vllm

echo "==> pre-downloading weights"
bash "$(dirname "$0")/download-model.sh" \
  "${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}" \
  "$WORKDIR/models/$(basename "${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}")" \
  "${MODELSCOPE_BASE:-https://modelscope.cn/models}" \
  "${DOWNLOAD_CONNECTIONS:-12}"

echo "==> vllm installed: $("$VENV/bin/python" -c 'import vllm; print(vllm.__version__)')"
echo "==> start it with:"
echo "    $VENV/bin/vllm serve $MODEL --host 0.0.0.0 --port $PORT \\"
echo "      --served-model-name $SERVED_NAME --max-model-len 4096 \\"
echo "      --gpu-memory-utilization 0.85 --max-num-seqs 64"

