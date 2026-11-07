#!/usr/bin/env bash
# Starts the vLLM OpenAI-compatible server inside WSL2.
set -euo pipefail

MODEL="${VLLM_MODEL:-Qwen/Qwen2.5-1.5B-Instruct}"
PORT="${VLLM_PORT:-8001}"
SERVED_NAME="${VLLM_SERVED_NAME:-$(basename "$MODEL" | tr 'A-Z' 'a-z')}"
MAX_LEN="${VLLM_MAX_MODEL_LEN:-4096}"
MAX_SEQS="${VLLM_MAX_NUM_SEQS:-64}"
GPU_UTIL="${VLLM_GPU_MEMORY_UTILIZATION:-0.85}"

WORKDIR="${WORKDIR:-/mnt/d/ai-infra}"
VENV="${VENV:-/opt/vllm-venv}"
if [ ! -x "$VENV/bin/python" ]; then
  VENV="$WORKDIR/.vllm-venv"
fi
export HF_HOME="${HF_HOME:-$WORKDIR/.hf-cache}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export VLLM_MODEL="$MODEL"
# The venv's bin holds helpers the JIT looks up by name (ninja).
export PATH="$VENV/bin:$PATH"

# The nvidia pip wheels ship nvcc and the CUDA headers, but not in the layout
# the CUDA toolchain expects. FlashInfer's JIT needs CUDA_HOME to find them.
if [ -z "${CUDA_HOME:-}" ]; then
  for candidate in "$VENV"/lib/python*/site-packages/nvidia/cu13 \
                   "$VENV"/lib/python*/site-packages/nvidia/cu12; do
    if [ -x "$candidate/bin/nvcc" ]; then
      export CUDA_HOME="$candidate"
      export PATH="$CUDA_HOME/bin:$PATH"
      break
    fi
  done
fi
echo "==> CUDA_HOME=${CUDA_HOME:-<unset>}"

# The nvcc that ships in the nvidia pip wheels does not match the CUDA
# headers flashinfer compiles against ("CUDA compiler and CUDA toolkit
# headers are incompatible"). Triton compiles its kernels without nvcc, so
# use it as the attention backend and keep flashinfer's sampler out of the
# picture. Set VLLM_ATTENTION_BACKEND=FLASHINFER when a full CUDA toolkit
# is installed instead.
export VLLM_ATTENTION_BACKEND="${VLLM_ATTENTION_BACKEND:-TRITON_ATTN}"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

# A local directory is passed straight through; a repo id is resolved by
# vLLM through HuggingFace (or the mirror above).
if [ -d "$MODEL" ]; then
  echo "==> serving local model directory: $MODEL"
else
  echo "==> serving model: $MODEL"
fi

exec "$VENV/bin/vllm" serve "$MODEL" \
  --host 0.0.0.0 \
  --port "$PORT" \
  --served-model-name "$SERVED_NAME" \
  --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-num-seqs "$MAX_SEQS" \
  --enable-prefix-caching

