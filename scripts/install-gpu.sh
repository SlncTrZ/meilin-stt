#!/usr/bin/env bash
# meilin-stt — cài đường GPU (TUỲ CHỌN, cần verify trên máy đích).
# Wing: ops | Topic: stt-install-gpu | Updated: 2026-10-31 23:07
#
# QUAN TRỌNG — đọc trước khi chạy:
#   1. Wheel `sherpa-onnx` trên PyPI là CPU-ONLY. Để ASR chạy CUDA phải build từ source
#      với SHERPA_ONNX_ENABLE_GPU=ON. Bước đó CHƯA được verify trên RTX 2080 tại repo này.
#   2. RTX 2080 = Turing (compute 7.5): có FP16, KHÔNG có BF16. Đừng bật bf16.
#   3. Máy đích chạy nhiều dịch vụ -> GPU là tài nguyên chia sẻ. Trước khi bật GPU cho
#      STT, đo bằng `python -m server.bench` và đọc docs/gpu-concurrency.md.
#
# Script này chỉ làm phần an toàn: cài onnxruntime-gpu + kiểm tra driver/CUDA.
# Phần build sherpa-onnx GPU in ra hướng dẫn, KHÔNG tự chạy (tránh hỏng môi trường).

set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Kiểm tra driver/CUDA"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap \
             --format=csv,noheader || true
else
  echo "❌ Không thấy nvidia-smi. Cài driver NVIDIA trước (khuyến nghị >= 550)." >&2
  exit 1
fi

echo "==> onnxruntime-gpu (chỉ dùng cho stage ONNX phụ trợ, không phải ASR)"
source .venv/bin/activate
pip install --upgrade "onnxruntime-gpu>=1.18" || {
  echo "⚠️  onnxruntime-gpu cài lỗi (thường do lệch CUDA/cuDNN). Bỏ qua — ASR vẫn chạy CPU." >&2
}

cat <<'EOF'

==> Build sherpa-onnx có CUDA (làm tay, KHÔNG tự động):

  sudo apt install -y cmake build-essential git
  git clone https://github.com/k2-fsa/sherpa-onnx /tmp/sherpa-onnx
  cd /tmp/sherpa-onnx
  export SHERPA_ONNX_CMAKE_ARGS="-DSHERPA_ONNX_ENABLE_GPU=ON -DCMAKE_CUDA_ARCHITECTURES=75"
  # 75 = Turing (RTX 20xx). Đổi theo compute_cap ở output nvidia-smi phía trên.
  pip install -e .            # hoặc: python setup.py bdist_wheel && pip install dist/*.whl
  cd -

  # Verify:
  python - <<'PY'
from sherpa_onnx.offline_recognizer import OfflineRecognizer
import inspect
print("provider param:", "provider" in inspect.signature(OfflineRecognizer.from_transducer).parameters)
PY

==> Sau khi build xong:

  # 1. Đo thật, đừng đoán:
  python -m server.bench --sample tests/fixtures/regression/sample.wav

  # 2. Chỉ apply khi GPU thật sự nhanh hơn VÀ máy còn headroom:
  python -m server.bench --apply

  # 3. Nếu máy đích bận GPU thường xuyên: giữ STT_DEVICE_*=cpu (mặc định).
  #    GPU chỉ nên dùng cho lane batch + dấu câu. Xem docs/gpu-concurrency.md.
EOF
