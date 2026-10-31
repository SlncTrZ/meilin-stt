"""Test engine — những chỗ đã từng gây lỗi thật khi tích hợp sherpa-onnx.

Wing: code | Topic: stt-tests-engine | Updated: 2026-10-31 23:35

Trọng tâm:
  - `ensure_bpe_vocab`: sherpa-onnx cần vocab TEXT (không phải bpe.model binary).
    Truyền binary vào -> tầng C++ thoát ngay, không có traceback Python. Đây là
    bug đã gặp khi smoke test, nên phải có test khoá lại.
  - `allow_gpu`: GPU bận/không có -> luôn fallback CPU, không raise.
  - `resample`: hàm tự viết, dễ sai độ dài.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pytest

from server import engine
from server.engine import Settings


# --- resample ---------------------------------------------------------------
def test_resample_identity_when_same_rate() -> None:
    audio = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    assert np.array_equal(engine.resample(audio, 16000, 16000), audio)


def test_resample_changes_length_proportionally() -> None:
    audio = np.zeros(16000, dtype=np.float32)
    assert engine.resample(audio, 16000, 8000).size == 8000
    assert engine.resample(audio, 8000, 16000).size == 32000


def test_resample_empty_is_safe() -> None:
    assert engine.resample(np.zeros(0, dtype=np.float32), 44100, 16000).size == 0


# --- device policy ----------------------------------------------------------
def test_allow_gpu_false_when_no_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "gpu_state", lambda: None)
    ok, reason = engine.allow_gpu(Settings())
    assert not ok and "nvidia-smi" in reason


def test_allow_gpu_false_when_vram_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "gpu_state", lambda: {"name": "x", "free_mb": 100, "total_mb": 8192, "util_pct": 5})
    settings = dataclasses.replace(Settings(), gpu_min_free_mb=1500)
    ok, reason = engine.allow_gpu(settings)
    assert not ok and "VRAM" in reason


def test_allow_gpu_false_when_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """GPU bận -> không dùng. Đây là bất biến với máy chia sẻ nhiều dịch vụ."""
    monkeypatch.setattr(engine, "gpu_state", lambda: {"name": "x", "free_mb": 7000, "total_mb": 8192, "util_pct": 85})
    ok, reason = engine.allow_gpu(dataclasses.replace(Settings(), gpu_max_util=40))
    assert not ok and "util" in reason


def test_allow_gpu_true_when_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "gpu_state", lambda: {"name": "x", "free_mb": 7000, "total_mb": 8192, "util_pct": 3})
    ok, reason = engine.allow_gpu(dataclasses.replace(Settings(), gpu_min_free_mb=1500, gpu_max_util=40))
    assert ok and "rảnh" in reason


# --- BPE vocab (bug thật: binary vs text) -----------------------------------
def test_ensure_bpe_vocab_empty_without_model_file(tmp_path: Path) -> None:
    assert engine.ensure_bpe_vocab(tmp_path) == ""


def test_write_vocab_is_two_columns() -> None:
    tmp = Path(__file__).resolve().parent / "_vocab_tmp.txt"
    try:
        engine._write_vocab(tmp, ["▁mei 0", "▁lin -1.5"])
        lines = tmp.read_text(encoding="utf-8").splitlines()
        assert lines == ["▁mei 0", "▁lin -1.5"]
        assert all(len(line.rsplit(" ", 1)) == 2 for line in lines)
    finally:
        tmp.unlink(missing_ok=True)


def test_ensure_bpe_vocab_generates_text_vocab_from_sentencepiece(tmp_path: Path) -> None:
    """Sinh vocab TEXT từ bpe.model — đây là thứ sherpa-onnx thực sự cần."""
    spm = pytest.importorskip("sentencepiece")
    corpus = tmp_path / "corpus.txt"
    corpus.write_text(
        "\n".join(["xin chào các bạn", "mở qdrant ra", "chạy nightly job", "kiểm tra hotwords"] * 30),
        encoding="utf-8",
    )
    prefix = tmp_path / "bpe"
    spm.SentencePieceTrainer.train(
        input=str(corpus),
        model_prefix=str(prefix),
        vocab_size=40,
        model_type="bpe",
        character_coverage=1.0,
        bos_id=-1,
        eos_id=-1,
        pad_id=-1,
        unk_id=0,
        minloglevel=3,
    )
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "bpe.model").write_bytes(Path(f"{prefix}.model").read_bytes())

    vocab_path = Path(engine.ensure_bpe_vocab(model_dir))
    assert vocab_path.exists() and vocab_path.name == "bpe.vocab"
    lines = [line for line in vocab_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 30, "phải export đủ vocab"
    assert all(len(line.rsplit(" ", 1)) == 2 for line in lines[:5]), "mỗi dòng phải là 'token score'"

    # idempotent: gọi lần 2 không sinh lại
    assert Path(engine.ensure_bpe_vocab(model_dir)) == vocab_path


# --- model readiness --------------------------------------------------------
def test_model_ready_detects_missing_files(tmp_path: Path) -> None:
    settings = dataclasses.replace(Settings(), model_dir_override=str(tmp_path))
    assert engine.model_ready(settings) is False
    (tmp_path / "tokens.txt").write_text("<blk> 0\n", encoding="utf-8")
    (tmp_path / "encoder.onnx").write_text("x", encoding="utf-8")
    assert engine.model_ready(settings) is True


def test_create_recognizer_raises_clear_error_on_missing_files(tmp_path: Path) -> None:
    settings = dataclasses.replace(Settings(), model_dir_override=str(tmp_path))
    with pytest.raises(FileNotFoundError) as exc:
        engine.create_recognizer(tmp_path, settings)
    assert "download-models.py" in str(exc.value), "lỗi phải chỉ được cách sửa"


def test_settings_summary_has_no_secret_fields() -> None:
    summary = engine.settings_summary(Settings())
    assert summary["model"]
    assert not any("token" in key or "secret" in key or "password" in key for key in summary)
