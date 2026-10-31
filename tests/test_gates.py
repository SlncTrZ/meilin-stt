"""Test regression gate — bất biến quan trọng nhất: thay đổi tệ hơn phải bị chặn.

Wing: code | Topic: stt-tests-gates | Updated: 2026-10-31 23:07
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

from nightly import gates


def _make_wav(path: Path, seconds: float = 0.2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def _manifest(tmp_path: Path, cases: list[dict[str, str]]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"cases": cases}, ensure_ascii=False), encoding="utf-8")
    return path


def test_normalize_text_strips_punctuation_and_case() -> None:
    assert gates.normalize_text("  Xin chào, Qdrant!  ") == "xin chào qdrant"


def test_cer_zero_for_identical_text() -> None:
    assert gates.cer("Xin chào", "xin chào.") == 0.0


def test_cer_one_for_completely_different() -> None:
    assert gates.cer("abc", "") == 1.0


def test_gate_passes_when_all_cases_within_threshold(tmp_path: Path) -> None:
    audio = _make_wav(tmp_path / "a.wav")
    manifest = _manifest(
        tmp_path, [{"id": "a", "audio": str(audio), "expect": "xin chào", "max_cer": 0.2}]
    )
    result = gates.run_gate(lambda _p: "xin chào", manifest=manifest)
    assert result.passed and result.mean_cer == 0.0


def test_gate_fails_when_case_exceeds_threshold(tmp_path: Path) -> None:
    audio = _make_wav(tmp_path / "a.wav")
    manifest = _manifest(
        tmp_path, [{"id": "a", "audio": str(audio), "expect": "xin chào các bạn", "max_cer": 0.1}]
    )
    result = gates.run_gate(lambda _p: "hoàn toàn khác", manifest=manifest)
    assert not result.passed
    assert result.reasons


def test_gate_fails_when_worse_than_baseline(tmp_path: Path) -> None:
    """Đây là bất biến của "train không phá": tệ hơn baseline + tolerance = chặn."""
    audio = _make_wav(tmp_path / "a.wav")
    manifest = _manifest(
        tmp_path, [{"id": "a", "audio": str(audio), "expect": "xin chào các bạn", "max_cer": 0.9}]
    )
    result = gates.run_gate(
        lambda _p: "xin chào các", baseline_mean_cer=0.0, tolerance=0.01, manifest=manifest
    )
    assert not result.passed
    assert any("baseline" in reason for reason in result.reasons)


def test_gate_skips_when_manifest_empty(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, [])
    result = gates.run_gate(lambda _p: "", manifest=manifest)
    assert result.passed, "chưa có bộ regression thì không chặn (ghi lý do rõ ràng)"
    assert result.reasons


def test_gate_fails_when_audio_missing(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path, [{"id": "a", "audio": str(tmp_path / "missing.wav"), "expect": "x"}]
    )
    result = gates.run_gate(lambda _p: "x", manifest=manifest)
    assert not result.passed


def test_edit_distance_bounds() -> None:
    assert gates.edit_distance("", "") == 0
    assert gates.edit_distance("abc", "abc") == 0
    assert gates.edit_distance("abc", "abd") == 1
    assert gates.edit_distance("abc", "abcd") == 1
