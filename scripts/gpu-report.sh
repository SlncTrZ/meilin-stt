#!/usr/bin/env bash
# meilin-stt — lấy mẫu headroom GPU/CPU để quyết định có nên dùng GPU hay không.
# Wing: ops | Topic: stt-gpu-report | Updated: 2026-10-31 23:07
#
#   ./scripts/gpu-report.sh            # 10 mẫu, 2s/lần
#   ./scripts/gpu-report.sh 30 5       # 30 mẫu, 5s/lần
#
# Log nối vào data/gpu-report.log để còn dữ liệu lịch sử khi cần quyết định.

set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
mkdir -p data
LOG="data/gpu-report.log"

SAMPLES="${1:-10}"
INTERVAL="${2:-2}"

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "Không có nvidia-smi — máy này chỉ dùng CPU." | tee -a "$LOG"
  exit 0
fi

echo "==> $SAMPLES mẫu, cách nhau ${INTERVAL}s — ghi $LOG"
for i in $(seq 1 "$SAMPLES"); do
  stamp="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  line="$(nvidia-smi --query-gpu=name,memory.used,memory.free,utilization.gpu \
                    --format=csv,noheader,nounits | head -1)"
  load="$(cut -d' ' -f1-3 /proc/loadavg)"
  printf '%s | %s | loadavg=%s\n' "$stamp" "$line" "$load" | tee -a "$LOG"
  [[ "$i" -lt "$SAMPLES" ]] && sleep "$INTERVAL"
done

echo
echo "==> Gate hiện tại:"
python -m nightly.gpu_gate --min-free-mb 1500 --max-util 20 --samples 3 --interval 1 || \
  echo "(gate fail = KHÔNG nên dùng GPU lúc này)"
