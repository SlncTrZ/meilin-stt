"""Bench thiết bị — chọn CPU/GPU theo SỐ ĐO, không theo cảm tính.

Wing: code | Topic: stt-bench | Updated: 2026-10-31 23:07

Chạy:
  python -m server.bench                 # đo và in bảng
  python -m server.bench --apply         # chốt vào data/device_profile.json

Kết quả dùng làm mặc định cho 2 lane (interactive/batch) trong engine.
GPU chỉ được đo khi `allow_gpu()` cho phép (máy chia sẻ -> thường fallback CPU).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from server import engine
from server.engine import Settings

SAMPLE_TEXT = "xin chào đây là bài kiểm tra tốc độ nhận dạng giọng nói tiếng việt"


def _load_audio(path: Path | None, seconds: float = 4.0) -> Any:
    import numpy as np

    if path and path.exists():
        import soundfile as sf

        audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            audio = engine.resample(audio, sr, 16000)
        return audio
    # Không có file mẫu: tạo tone + nhiễu để đo thuần tốc độ (không đo độ chính xác)
    rng = np.random.default_rng(0)
    n = int(seconds * 16000)
    t = np.arange(n) / 16000.0
    return (0.05 * np.sin(2 * np.pi * 220 * t) + 0.01 * rng.standard_normal(n)).astype(np.float32)


def measure(provider: str, audio: Any, settings: Settings, runs: int = 3) -> dict[str, Any]:
    """Đo thời gian load + độ trễ decode trung bình/p95."""
    import numpy as np

    started = time.monotonic()
    rec = engine.create_recognizer(settings.model_dir, settings, provider=provider)
    load_sec = time.monotonic() - started

    latencies: list[float] = []
    text = ""
    for _ in range(runs):
        t0 = time.monotonic()
        stream = rec.create_stream()
        stream.accept_waveform(16000, audio)
        rec.decode_stream(stream)
        latencies.append((time.monotonic() - t0) * 1000.0)
        text = (stream.result.text or "").strip()

    duration = len(audio) / 16000.0
    mean_ms = statistics.fmean(latencies)
    p95 = sorted(latencies)[max(0, int(round(0.95 * len(latencies))) - 1)]
    return {
        "provider": provider,
        "load_sec": round(load_sec, 2),
        "audio_sec": round(duration, 2),
        "mean_ms": round(mean_ms, 1),
        "p95_ms": round(p95, 1),
        "rtf": round(mean_ms / 1000.0 / duration, 4) if duration else None,
        "chars": len(text),
        "sample_text": text[:120],
        "sane": bool(text) or np.mean(np.abs(audio)) < 0.001,
    }


def run(sample: Path | None, apply: bool, runs: int) -> int:
    settings = Settings()
    if not engine.model_ready(settings):
        print(
            f"❌ Chưa có model trong {settings.model_dir}. "
            f"Chạy: python scripts/download-models.py --model {settings.model_version}",
            file=sys.stderr,
        )
        return 2

    audio = _load_audio(sample)
    results: dict[str, Any] = {"model": settings.model_cfg["label"], "runs": runs, "lanes": {}}

    print(f"Model: {settings.model_cfg['label']}  audio={len(audio) / 16000:.1f}s  runs={runs}")
    cpu = measure("cpu", audio, settings, runs)
    results["lanes"]["cpu"] = cpu
    print(f"  cpu : load {cpu['load_sec']}s  mean {cpu['mean_ms']}ms  p95 {cpu['p95_ms']}ms  rtf {cpu['rtf']}")

    gpu_ok, reason = engine.allow_gpu(settings)
    if gpu_ok:
        try:
            gpu = measure("cuda", audio, settings, runs)
            results["lanes"]["cuda"] = gpu
            print(f"  cuda: load {gpu['load_sec']}s  mean {gpu['mean_ms']}ms  p95 {gpu['p95_ms']}ms  rtf {gpu['rtf']}")
        except Exception as exc:  # noqa: BLE001 - wheel CPU-only sẽ raise ở đây
            results["gpu_error"] = str(exc)
            print(f"  cuda: KHÔNG dùng được ({exc})")
    else:
        results["gpu_skip_reason"] = reason
        print(f"  cuda: bỏ qua ({reason})")

    # Chọn lane: interactive ưu tiên p95 thấp; batch ưu tiên throughput.
    fastest = min(
        results["lanes"].items(), key=lambda kv: kv[1]["p95_ms"]
    )
    results["recommend"] = {
        "interactive": fastest[0],
        "batch": fastest[0],
    }
    print(f"  → đề xuất: {results['recommend']}")

    if apply:
        profile = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "interactive": results["recommend"]["interactive"],
            "batch": results["recommend"]["batch"],
            "detail": results,
        }
        path = engine.save_device_profile(profile)
        print(f"Đã ghi profile: {path}")
    else:
        print("(chưa apply — thêm --apply để ghi data/device_profile.json)")
    if apply:
        print(json.dumps(results["recommend"], ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meilin-stt-bench", description=__doc__)
    parser.add_argument("--sample", type=Path, default=None, help="file wav mẫu (nên 3-10s)")
    parser.add_argument("--apply", action="store_true", help="ghi device_profile.json")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    return run(args.sample, args.apply, args.runs)


if __name__ == "__main__":
    sys.exit(main())
