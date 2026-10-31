"""Regression gate — chốt chặn để "train" không làm model tệ đi.

Wing: code | Topic: stt-nightly-gates | Updated: 2026-10-31 23:07

Không có gate này thì việc thêm hàng trăm hotword sẽ khiến model *bịa* từ khoá
khi audio nhiễu. Gate so CER trên bộ case đóng băng; không đạt → không promote.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("meilin-stt.gates")

BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST = BASE_DIR / "tests" / "fixtures" / "regression" / "manifest.json"


def normalize_text(text: str) -> str:
    """Chuẩn hoá trước khi tính CER: NFC, lowercase, gộp khoảng trắng, bỏ dấu câu."""
    text = unicodedata.normalize("NFC", text or "").lower()
    kept = [ch if (ch.isalnum() or ch.isspace()) else " " for ch in text]
    return " ".join("".join(kept).split())


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> float:
    """Character Error Rate trên text đã chuẩn hoá."""
    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    cer: float
    max_cer: float
    reference: str
    hypothesis: str

    @property
    def ok(self) -> bool:
        return self.cer <= self.max_cer


@dataclass
class GateResult:
    passed: bool
    mean_cer: float
    baseline_mean_cer: float | None
    tolerance: float
    cases: list[CaseResult]
    reasons: list[str]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["cases"] = [asdict(c) for c in self.cases]
        return data


def load_manifest(path: Path = MANIFEST) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Manifest regression hỏng: %s", exc)
        return []
    return list(payload.get("cases", []))


def run_gate(
    transcribe_fn: Callable[[Path], str],
    baseline_mean_cer: float | None = None,
    tolerance: float = 0.01,
    manifest: Path = MANIFEST,
) -> GateResult:
    """Chạy toàn bộ case. `transcribe_fn(path) -> text` do caller cung cấp."""
    cases = load_manifest(manifest)
    if not cases:
        return GateResult(
            passed=True,
            mean_cer=0.0,
            baseline_mean_cer=baseline_mean_cer,
            tolerance=tolerance,
            cases=[],
            reasons=["manifest rỗng — gate bỏ qua (chưa có bộ regression)"],
        )

    results: list[CaseResult] = []
    reasons: list[str] = []
    for case in cases:
        audio = Path(case["audio"])
        if not audio.is_absolute():
            audio = BASE_DIR / audio
        if not audio.exists():
            reasons.append(f"{case.get('id', audio.name)}: thiếu file audio {audio}")
            continue
        try:
            hypothesis = transcribe_fn(audio)
        except Exception as exc:  # noqa: BLE001 - lỗi decode không làm sập job
            reasons.append(f"{case.get('id', audio.name)}: transcribe lỗi {exc}")
            continue
        value = cer(case.get("expect", ""), hypothesis)
        results.append(
            CaseResult(
                case_id=str(case.get("id", audio.stem)),
                cer=round(value, 4),
                max_cer=float(case.get("max_cer", 0.15)),
                reference=case.get("expect", ""),
                hypothesis=hypothesis,
            )
        )

    if not results:
        return GateResult(
            passed=False,
            mean_cer=1.0,
            baseline_mean_cer=baseline_mean_cer,
            tolerance=tolerance,
            cases=[],
            reasons=reasons or ["không chạy được case nào"],
        )

    mean_cer = sum(r.cer for r in results) / len(results)
    for result in results:
        if not result.ok:
            reasons.append(
                f"{result.case_id}: CER {result.cer:.3f} > ngưỡng {result.max_cer:.3f}"
            )
    if baseline_mean_cer is not None and mean_cer > baseline_mean_cer + tolerance:
        reasons.append(
            f"mean CER {mean_cer:.4f} tệ hơn baseline {baseline_mean_cer:.4f} "
            f"+{tolerance:.4f}"
        )
    return GateResult(
        passed=not reasons,
        mean_cer=round(mean_cer, 4),
        baseline_mean_cer=baseline_mean_cer,
        tolerance=tolerance,
        cases=results,
        reasons=reasons,
    )
