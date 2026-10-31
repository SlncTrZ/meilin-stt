"""Test nightly runner — thứ tự cố định, gate chặn, dry-run không publish.

Wing: code | Topic: stt-tests-runner | Updated: 2026-10-31 23:07
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from nightly import runner


@pytest.fixture()
def patched_settings(monkeypatch: pytest.MonkeyPatch, fake_model_dir: Path):
    from server import engine

    base = engine.Settings()
    custom = dataclasses.replace(base, model_dir_override=str(fake_model_dir))
    monkeypatch.setattr(engine, "Settings", lambda: custom)
    return custom


def test_dry_run_does_not_publish(sample_pairs: dict[str, Path], patched_settings) -> None:
    data = runner.run(dry_run=True, force=True, skip_regression=True, keep=5)
    assert data["published"] is False
    assert "Dry-run" in data["message"]
    assert pipeline_current() is None, "dry-run không được đổi con trỏ current"
    assert Path(data["artifacts"]["hotwords_path"]).exists()


def test_gate_fail_keeps_old_pointer(
    sample_pairs: dict[str, Path], patched_settings, isolated_data: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from lexicon import pipeline
    from nightly import gates

    # Dựng artifact "đang chạy" cũ
    old = isolated_data["artifacts"] / "20261001-0300"
    old.mkdir(parents=True, exist_ok=True)
    (old / "hotwords.txt").write_text("CŨ :2.5\n", encoding="utf-8")
    pipeline.publish_artifacts("20261001-0300")

    # Ép gate fail
    failing = gates.GateResult(
        passed=False,
        mean_cer=0.5,
        baseline_mean_cer=0.1,
        tolerance=0.01,
        cases=[],
        reasons=["ép fail trong test"],
    )
    monkeypatch.setattr(gates, "load_manifest", lambda *_a, **_k: [{"id": "x"}])
    monkeypatch.setattr(gates, "run_gate", lambda *_a, **_k: failing)

    data = runner.run(force=True, skip_regression=False, keep=5)
    assert data["published"] is False
    assert data["rollback_to"] == "20261001-0300"
    assert pipeline.current_stamp() == "20261001-0300", "gate fail phải giữ artifact cũ"


def test_gate_pass_publishes(
    sample_pairs: dict[str, Path], patched_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lexicon import pipeline
    from nightly import gates

    passing = gates.GateResult(
        passed=True,
        mean_cer=0.02,
        baseline_mean_cer=0.03,
        tolerance=0.01,
        cases=[],
        reasons=[],
    )
    monkeypatch.setattr(gates, "load_manifest", lambda *_a, **_k: [{"id": "x"}])
    monkeypatch.setattr(gates, "run_gate", lambda *_a, **_k: passing)

    data = runner.run(force=True, skip_regression=False, keep=5)
    assert data["published"] is True
    assert pipeline.current_stamp() == data["stamp"]
    assert data["report_path"]


def test_resource_gate_skip_writes_report(
    sample_pairs: dict[str, Path], patched_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nightly import gpu_gate

    monkeypatch.setattr(
        gpu_gate,
        "evaluate",
        lambda **_kw: {"ok": False, "reasons": ["ép fail trong test"], "gpu": [], "cpu_free_pct": 10.0, "mem_free_gb": 0.2},
    )
    data = runner.run(force=False, skip_regression=True)
    assert data["published"] is False
    assert "headroom" in data["message"]
    assert data.get("report_path")


def pipeline_current():
    from lexicon import pipeline

    return pipeline.current_stamp()
