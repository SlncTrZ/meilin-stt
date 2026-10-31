"""Trạm gác tài nguyên trước khi job nền chạm CPU/GPU của máy chia sẻ.

Wing: code | Topic: stt-nightly-gate | Updated: 2026-10-31 23:07

Dùng làm `ExecCondition=` của systemd: exit 0 = được chạy, exit 1 = bỏ qua.
Triết lý: máy này chạy nhiều dịch vụ khác. Job của ta là khách — không đủ
headroom thì **không chạy**, và systemd sẽ chạy bù đêm sau (`Persistent=true`).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

# Console Windows mac dinh cp1252 -> ep UTF-8 de log tieng Viet khong crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import time


def gpu_samples(count: int, interval: float) -> list[dict[str, float]]:
    """Lấy `count` mẫu nvidia-smi, cách nhau `interval` giây."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out: list[dict[str, float]] = []
    for i in range(max(1, count)):
        try:
            proc = subprocess.run(
                [
                    exe,
                    "--query-gpu=memory.free,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            first = proc.stdout.strip().splitlines()[0]
            free_mb, util = (float(p.strip()) for p in first.split(",")[:2])
            out.append({"free_mb": free_mb, "util_pct": util})
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            continue
        if i + 1 < count:
            time.sleep(interval)
    return out


def cpu_free_pct() -> float:
    """% CPU rảnh trung bình 1s (không cần psutil)."""
    try:
        import os

        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        return 100.0
    cores = max(1, len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else 4)
    busy = min(100.0, (load1 / cores) * 100.0)
    return round(100.0 - busy, 1)


def mem_free_gb() -> float:
    """RAM trống (GB) đọc từ /proc/meminfo; Windows/không có → coi như thoải mái."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 2)
    except OSError:
        pass
    return 999.0


def evaluate(
    *,
    min_free_mb: int = 3000,
    max_util: int = 20,
    samples: int = 5,
    interval: float = 2.0,
    min_free_cpu_pct: float = 30.0,
    min_free_mem_gb: float = 0.5,
    require_gpu: bool = False,
) -> dict:
    """Chấm headroom và trả payload (dùng được cả từ CLI lẫn nightly runner)."""
    reasons: list[str] = []
    gpu = gpu_samples(samples, interval)
    if not gpu:
        if require_gpu:
            reasons.append("không có GPU / nvidia-smi")
    else:
        worst_free = min(s["free_mb"] for s in gpu)
        worst_util = max(s["util_pct"] for s in gpu)
        if worst_free < min_free_mb:
            reasons.append(f"VRAM trống thấp nhất {worst_free:.0f}MB < {min_free_mb}MB")
        if worst_util > max_util:
            reasons.append(f"GPU util cao nhất {worst_util:.0f}% > {max_util}%")

    free_cpu = cpu_free_pct()
    free_mem = mem_free_gb()
    if free_cpu < min_free_cpu_pct:
        reasons.append(f"CPU rảnh {free_cpu}% < {min_free_cpu_pct}%")
    if free_mem < min_free_mem_gb:
        reasons.append(f"RAM trống {free_mem}GB < {min_free_mem_gb}GB")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "gpu": gpu,
        "cpu_free_pct": free_cpu,
        "mem_free_gb": free_mem,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate tài nguyên cho job nền meilin-stt")
    parser.add_argument("--min-free-mb", type=int, default=3000, help="VRAM trống tối thiểu")
    parser.add_argument("--max-util", type=int, default=20, help="GPU util tối đa cho phép")
    parser.add_argument("--samples", type=int, default=5, help="số mẫu GPU")
    parser.add_argument("--interval", type=float, default=2.0, help="giây giữa các mẫu")
    parser.add_argument("--min-free-cpu-pct", type=float, default=30.0)
    parser.add_argument("--min-free-mem-gb", type=float, default=0.5)
    parser.add_argument("--require-gpu", action="store_true", help="không có GPU = fail")
    parser.add_argument("--json", action="store_true", help="in JSON thay vì text")
    args = parser.parse_args(argv)

    payload = evaluate(
        min_free_mb=args.min_free_mb,
        max_util=args.max_util,
        samples=args.samples,
        interval=args.interval,
        min_free_cpu_pct=args.min_free_cpu_pct,
        min_free_mem_gb=args.min_free_mem_gb,
        require_gpu=args.require_gpu,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif payload["reasons"]:
        print("SKIP: " + "; ".join(payload["reasons"]))
    else:
        last = payload["gpu"][-1] if payload["gpu"] else "none"
        print(f"OK: GPU={last} CPU_free={payload['cpu_free_pct']}% RAM_free={payload['mem_free_gb']}GB")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
