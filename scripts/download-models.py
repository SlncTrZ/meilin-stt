"""Tải model ASR về máy (idempotent, có thể chạy trong Docker build).

Wing: code | Topic: stt-scripts | Updated: 2026-10-31 23:07

Chạy:
  python scripts/download-models.py                  # model mặc định (68m)
  python scripts/download-models.py --model 30m       # model nhẹ (int8, ~33MB)
  python scripts/download-models.py --model all       # tải cả hai
  python scripts/download-models.py --check           # chỉ kiểm tra đã có chưa
"""

from __future__ import annotations

import argparse
import sys

# Console Windows mặc định cp1252 -> ép UTF-8 để log tiếng Việt không crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from server import engine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tải model sherpa-onnx cho meilin-stt")
    parser.add_argument("--model", default=None, choices=[*engine.MODEL_REGISTRY, "all"])
    parser.add_argument("--check", action="store_true", help="chỉ kiểm tra, không tải")
    args = parser.parse_args(argv)

    settings = engine.Settings()
    versions = list(engine.MODEL_REGISTRY) if args.model == "all" else [args.model or settings.model_version]
    rc = 0
    for version in versions:
        cfg = engine.MODEL_REGISTRY[version]
        model_dir = Path.home() / ".cache" / "sherpa-onnx" / cfg["dir"]
        print(f"[{version}] {cfg['label']} → {model_dir}")
        if args.check:
            ok = model_dir.exists() and any(model_dir.glob("encoder*.onnx"))
            print(f"[{version}] {'✅ có sẵn' if ok else '❌ thiếu'}")
            rc = rc or (0 if ok else 1)
            continue
        try:
            path = engine.download_model(settings, version=version)
            print(f"[{version}] ✅ xong: {path}")
        except Exception as exc:  # noqa: BLE001 - báo lỗi rõ cho người chạy
            print(f"[{version}] ❌ lỗi: {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
