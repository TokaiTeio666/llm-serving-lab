#!/usr/bin/env bash
# Downloads a HuggingFace-style model directory with parallel range requests.
#
# Single-connection throughput to the model CDNs from this network is
# ~0.3-1.5 MB/s, which makes a 3 GB file take hours. Splitting the file into
# parallel byte ranges multiplies that by the number of connections.
#
# Usage:
#   bash download-model.sh <repo-id> <target-dir> [base-url] [connections]
#
#   repo-id      e.g. Qwen/Qwen2.5-1.5B-Instruct
#   target-dir   e.g. /mnt/d/ai-infra/models/Qwen2.5-1.5B-Instruct
#   base-url     default https://modelscope.cn/models
set -euo pipefail

REPO="${1:?repo id required, e.g. Qwen/Qwen2.5-1.5B-Instruct}"
TARGET="${2:?target dir required}"
BASE="${3:-https://modelscope.cn/models}"
CONNECTIONS="${4:-12}"
FILE_LIST="${5:-}"

if [ -n "$FILE_LIST" ]; then
  IFS=',' read -r -a FILES <<< "$FILE_LIST"
else
  FILES=(
    config.json
    generation_config.json
    merges.txt
    added_tokens.json
    special_tokens_map.json
    tokenizer.json
    tokenizer_config.json
    vocab.json
    model.safetensors
  )
fi

mkdir -p "$TARGET"

expected_sha() {
  # Pull the published sha256 for one file out of the ModelScope file API.
  local repo="$1" name="$2"
  curl -sSL "${BASE}/${repo}/repo/files?Revision=master&Root=" \
    | python3 -c '
import json, sys
name = sys.argv[1]
for entry in json.load(sys.stdin).get("Data", {}).get("Files", []):
    if entry.get("Name") == name:
        print(entry.get("Sha256", ""))
        break
' "$name" 2>/dev/null || true
}

sha256_of() {
  sha256sum "$1" | cut -d" " -f1
}

content_length() {
  # HEAD returns the compressed length, which does not match the raw bytes we
  # splice together. A one-byte range probe reports the true total in
  # Content-Range: bytes 0-0/<total>.
  curl -sSL -r 0-0 -D - -o /dev/null -H "Accept-Encoding: identity" "$1" \
    | tr -d '\r' \
    | awk 'tolower($1)=="content-range:"{n=$NF} END{print n}' \
    | sed 's#.*/##'
}

download_with_ranges() {
  local url="$1" out="$2" size="$3" parts="$4"
  if [ "$size" -le 0 ]; then
    curl -sSL --retry 8 --retry-delay 3 -o "$out" "$url"
    return
  fi
  local chunk=$(( (size + parts - 1) / parts ))
  local pids=() i start end part want have
  for i in $(seq 0 $(( parts - 1 ))); do
    start=$(( i * chunk ))
    end=$(( start + chunk - 1 ))
    [ "$end" -ge "$size" ] && end=$(( size - 1 ))
    [ "$start" -ge "$size" ] && break
    part="${out}.part${i}"
    want=$(( end - start + 1 ))
    have=0
    [ -f "$part" ] && have=$(stat -c %s "$part")
    if [ "$have" -ge "$want" ]; then
      continue
    fi
    # Resume a partial chunk by asking for the remaining byte range. The
    # remainder lands in a temp file first: --retry restarts the range from
    # scratch, so appending straight to the part would duplicate bytes.
    # --speed-limit aborts a connection that stops delivering -- these CDNs
    # happily leave one open forever with no bytes flowing.
    if [ "$have" -gt 0 ]; then
      range_start=$(( start + have ))
    else
      range_start=$start
    fi
    curl -sSL --retry 8 --retry-delay 3 --retry-all-errors \
      -H "Accept-Encoding: identity" \
      --speed-limit 20480 --speed-time 30 --max-time 900 \
      -r "${range_start}-${end}" -o "${part}.tmp" "$url" &
    pids+=($!)
  done
  local failed=0
  for p in "${pids[@]}"; do wait "$p" || failed=1; done
  if [ "$failed" -ne 0 ]; then
    echo "FAILED: $out" >&2
    return 1
  fi
  # Fold the freshly downloaded remainder into each part.
  for i in $(seq 0 $(( parts - 1 ))); do
    if [ -f "${out}.part${i}.tmp" ]; then
      cat "${out}.part${i}.tmp" >> "${out}.part${i}"
      rm -f "${out}.part${i}.tmp"
    fi
  done
  : > "$out"
  for i in $(seq 0 $(( parts - 1 ))); do
    if [ -f "${out}.part${i}" ]; then
      cat "${out}.part${i}" >> "$out"
    fi
  done
  rm -f "${out}".part*
  local actual
  actual=$(stat -c %s "$out")
  if [ "$actual" -ne "$size" ]; then
    echo "SIZE MISMATCH for $out: got $actual want $size" >&2
    return 1
  fi
}

for name in "${FILES[@]}"; do
  url="${BASE}/${REPO}/resolve/master/${name}"
  out="${TARGET}/${name}"
  if [ -s "$out" ]; then
    want_sha=$(expected_sha "$REPO" "$name")
    if [ -n "$want_sha" ] && [ "$(sha256_of "$out")" = "$want_sha" ]; then
      echo "skip $name (present, checksum ok)"
      continue
    fi
    echo "re-downloading $name (present but checksum mismatch)"
    rm -f "$out"
  fi
  size=$(content_length "$url")
  echo "downloading $name ($(( size / 1024 / 1024 )) MB) with ${CONNECTIONS} connections"
  download_with_ranges "$url" "$out" "${size:-0}" "$CONNECTIONS"
  want_sha=$(expected_sha "$REPO" "$name")
  if [ -n "$want_sha" ]; then
    got_sha=$(sha256_of "$out")
    if [ "$got_sha" != "$want_sha" ]; then
      echo "CHECKSUM MISMATCH for $name" >&2
      echo "  want $want_sha" >&2
      echo "  got  $got_sha" >&2
      exit 1
    fi
    echo "  -> $name checksum verified"
  fi
  echo "  -> $name done"
done

echo "model ready at $TARGET"

