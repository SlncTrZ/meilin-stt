#!/usr/bin/env bash
# meilin-stt — cài đặt trên Linux (bare metal, không Docker).
# Wing: ops | Topic: stt-install | Updated: 2026-10-31 23:07
#
#   ./scripts/install.sh                 # cài + tải model 68m + seed hotwords
#   ./scripts/install.sh --model 30m     # máy RAM thấp, model int8 nhẹ
#   ./scripts/install.sh --no-seed       # không ghi đè lexicon/hotwords hiện có
#
# Idempotent: chạy lại an toàn. KHÔNG cần quyền root cho bước cài app;
# chỉ bước systemd mới cần sudo (xem cuối output).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

MODEL="68m"
SEED=1
PY="${PYTHON:-python3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --no-seed) SEED=0; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Tham số lạ: $1" >&2; exit 2 ;;
  esac
done

echo "==> Repo: $REPO_DIR"
echo "==> Python: $($PY --version)"

if [[ ! -d .venv ]]; then
  echo "==> Tạo venv .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel >/dev/null

echo "==> Cài package (CPU-only)"
pip install -e .

echo "==> Tải model: $MODEL"
python scripts/download-models.py --model "$MODEL"

if [[ "$SEED" == "1" ]]; then
  echo "==> Seed hotwords phân tier"
  python scripts/seed_hotwords.py --merge
  MODEL_DIR="$(python - <<'PY'
import sys
sys.path.insert(0, ".")
from server import engine
print(engine.Settings().model_dir)
PY
)"
  echo "==> Validate hotwords bằng BPE của model ($MODEL_DIR)"
  python -m lexicon.cli validate --model-dir "$MODEL_DIR" || \
    echo "⚠️  Có hotword không emit được — xem output trên, cân nhắc demote."
fi

echo "==> Smoke test"
python - <<'PY'
import sys
sys.path.insert(0, ".")
from server import engine
s = engine.Settings()
print("model      :", s.model_cfg["label"])
print("model_dir  :", s.model_dir)
print("model_ready:", engine.model_ready(s))
print("gpu        :", engine.gpu_state() or "none")
ok, reason = engine.allow_gpu(s)
print("allow_gpu  :", ok, "-", reason)
PY

cat <<'EOF'

==> Xong phần app. Bước tiếp theo (cần sudo):

  1. Tạo user + thư mục:
       sudo useradd -r -m -d /opt/meilin-stt meilin   # nếu chưa có
       sudo mkdir -p /etc/meilin-stt
       sudo cp .env.example /etc/meilin-stt/stt.env
       sudo chmod 600 /etc/meilin-stt/stt.env
       sudo chown -R meilin:meilin data models

  2. Cài service + timer:
       sudo cp deploy/meilin-stt.service deploy/meilin-stt-nightly.service \
               deploy/meilin-stt-nightly.timer /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now meilin-stt.service
       sudo systemctl enable --now meilin-stt-nightly.timer

  3. Kiểm tra:
       systemctl status meilin-stt --no-pager
       journalctl -u meilin-stt -n 50 --no-pager
       curl -s http://127.0.0.1:8766/health | python -m json.tool

  4. Đo thiết bị (chọn CPU/GPU theo số đo):
       .venv/bin/python -m server.bench --apply
EOF
