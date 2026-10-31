"""Nightly runner — điều phối vòng học đêm: mine → render → gate → publish/rollback.

Wing: code | Topic: stt-nightly-runner | Updated: 2026-10-31 23:07

Thứ tự cố định (không đổi được bằng config, chỉ tắt được bằng flag):
  1. gate tài nguyên (CPU/RAM/GPU) — máy chia sẻ thì bỏ qua, không chen ngang
  2. mine + promote lexicon (CPU)
  3. render artifact ứng viên vào `data/artifacts/<stamp>/`
  4. regression gate (nếu có bộ case)
  5. pass  -> publish con trỏ `current` (server reload nhờ SIGHUP/systemd)
     fail  -> giữ nguyên con trỏ cũ (tự động rollback), ghi report ❌
  6. dọn artifact cũ (retention)
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lexicon import pipeline
from nightly import gates, gpu_gate, report

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meilin-stt.nightly")

BASE_DIR = Path(__file__).resolve().parent.parent


def _baseline_cer() -> float | None:
    """mean CER của artifact đang chạy (đọc metrics.json)."""
    metrics = pipeline.current_artifact("metrics.json")
    if not metrics:
        return None
    try:
        return float(json.loads(metrics.read_text(encoding="utf-8")).get("mean_cer"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _make_transcribe_fn(hotwords_path: Path) -> Callable[[Path], str]:
    """Hàm transcribe 1 file bằng hotwords ứng viên (dùng cho gate)."""
    from server import engine

    settings = engine.Settings()

    def transcribe(audio_path: Path) -> str:
        import soundfile as sf

        audio, sr = sf.read(str(audio_path), dtype="float32", always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            audio = engine.resample(audio, sr, 16000)
        rec = engine.create_recognizer(
            settings.model_dir,
            settings,
            provider="cpu",
            hotwords_file=str(hotwords_path) if hotwords_path.exists() else "",
            hotwords_score=settings.hotwords_score,
        )
        stream = rec.create_stream()
        stream.accept_waveform(16000, audio)
        rec.decode_stream(stream)
        return (stream.result.text or "").strip()

    return transcribe


def run(
    *,
    dry_run: bool = False,
    min_hits: int = 2,
    min_support: int = 2,
    min_support_rule: int = 1,
    skip_regression: bool = False,
    tolerance: float = 0.01,
    keep: int = 14,
    force: bool = False,
    require_gpu: bool = False,
) -> dict[str, Any]:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    stamp = pipeline.stamp_now()
    data: dict[str, Any] = {
        "started_at": started_at,
        "stamp": stamp,
        "dry_run": dry_run,
        "published": False,
        "rollback_to": None,
    }

    # 1. Gate tài nguyên ----------------------------------------------------
    resource = gpu_gate.evaluate(
        min_free_mb=1500,
        max_util=20,
        samples=3,
        interval=1.0,
        min_free_cpu_pct=25.0,
        min_free_mem_gb=0.5,
        require_gpu=require_gpu,
    )
    data["resource_gate"] = resource
    if not resource["ok"] and not force:
        data["message"] = (
            "Bỏ qua đêm nay: không đủ headroom tài nguyên ("
            + "; ".join(resource["reasons"])
            + "). systemd sẽ chạy bù đêm sau."
        )
        data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        data["report_path"] = str(report.write_report(data))
        log.warning(data["message"])
        return data

    # 2. Mine + promote -----------------------------------------------------
    if dry_run:
        data["mine"] = {"pairs": len(pipeline.collect_pairs()), "new": 0, "dry_run": True}
        data["promoted"] = 0
    else:
        data["mine"] = pipeline.run_mine(min_support=min_support)
        data["promoted"] = pipeline.run_promote(min_hits=min_hits)
    log.info("mine=%s promoted=%s", data["mine"], data["promoted"])

    # 3. Render artifact ứng viên ------------------------------------------
    from server import engine

    settings = engine.Settings()
    snapshot = pipeline.ARTIFACTS_DIR / stamp
    artifacts = pipeline.render_artifacts(
        model_dir=settings.model_dir,
        out_dir=snapshot,
        include_candidates=False,
        min_support_rule=min_support_rule,
    )
    data["artifacts"] = artifacts
    log.info("artifact ứng viên: %s", artifacts)

    # 4. Regression gate ----------------------------------------------------
    baseline = _baseline_cer()
    if skip_regression or not gates.load_manifest():
        gate_result = gates.GateResult(
            passed=True,
            mean_cer=0.0,
            baseline_mean_cer=baseline,
            tolerance=tolerance,
            cases=[],
            reasons=["regression gate bị tắt hoặc chưa có bộ case"],
        )
    else:
        transcribe_fn = _make_transcribe_fn(snapshot / "hotwords.txt")
        gate_result = gates.run_gate(
            transcribe_fn, baseline_mean_cer=baseline, tolerance=tolerance
        )
    data["gate"] = gate_result.to_json()
    log.info("gate passed=%s mean_cer=%s", gate_result.passed, gate_result.mean_cer)

    (snapshot / "metrics.json").write_text(
        json.dumps(
            {
                "generated_at": pipeline.now_ts(),
                "mean_cer": gate_result.mean_cer,
                "baseline_mean_cer": baseline,
                "passed": gate_result.passed,
                "hotwords_terms": artifacts["hotwords_terms"],
                "rules": artifacts["rules"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 5. Publish hoặc rollback ---------------------------------------------
    if gate_result.passed and not dry_run:
        pipeline.publish_artifacts(stamp)
        data["published"] = True
        data["message"] = (
            f"Đã publish artifact {stamp}: {artifacts['hotwords_terms']} hotword, "
            f"{artifacts['rules']} rule. mean CER {gate_result.mean_cer:.4f}."
        )
    elif dry_run:
        data["message"] = f"Dry-run: artifact nằm ở {snapshot}, chưa publish."
    else:
        previous = pipeline.current_stamp()
        pipeline.rollback_to(previous)
        data["rollback_to"] = previous
        data["message"] = "Gate fail — giữ artifact cũ, không publish."
        shutil.rmtree(snapshot, ignore_errors=True)

    # 6. Retention ----------------------------------------------------------
    data["removed_artifacts"] = pipeline.retention(keep=keep)

    data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = report.write_report(data)
    data["report_path"] = str(path)
    log.info("report: %s", path)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meilin-stt-nightly", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="không ghi lexicon/publish")
    parser.add_argument("--min-hits", type=int, default=2)
    parser.add_argument("--min-support", type=int, default=2)
    parser.add_argument("--min-support-rule", type=int, default=1)
    parser.add_argument("--skip-regression", action="store_true")
    parser.add_argument("--tolerance", type=float, default=0.01)
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--force", action="store_true", help="bỏ qua gate tài nguyên")
    parser.add_argument("--require-gpu", action="store_true")
    parser.add_argument("--json", action="store_true", help="in JSON kết quả")
    args = parser.parse_args(argv)

    data = run(
        dry_run=args.dry_run,
        min_hits=args.min_hits,
        min_support=args.min_support,
        min_support_rule=args.min_support_rule,
        skip_regression=args.skip_regression,
        tolerance=args.tolerance,
        keep=args.keep,
        force=args.force,
        require_gpu=args.require_gpu,
    )
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    ok = bool(data.get("published") or data.get("dry_run"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
