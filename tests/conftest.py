"""Fixtures dùng chung cho test (không cần model thật, không đụng data/ thật).

Wing: code | Topic: stt-tests | Updated: 2026-10-31 23:07
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


@pytest.fixture()
def fake_model_dir(tmp_path: Path) -> Path:
    """Model dir giả: có tokens.txt, KHÔNG có bpe.model.

    `render.validate_entries` coi "không tokenize được" là "không kiểm chứng được"
    -> giữ entry. Đủ để test luồng render/pipeline mà không cần tải model thật.
    """
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "tokens.txt").write_text("<blk> 0\n<unk> 1\n", encoding="utf-8")
    return model_dir


@pytest.fixture()
def isolated_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Cô lập data/ + models/ + artifacts để test không đụng dữ liệu thật."""
    from lexicon import pipeline, store

    data_dir = tmp_path / "data"
    models_dir = tmp_path / "models"
    artifacts = data_dir / "artifacts"
    for path in (data_dir, models_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(store, "DATA_DIR", data_dir)
    monkeypatch.setattr(store, "UTTERANCES", data_dir / "utterances.jsonl")
    monkeypatch.setattr(store, "FEEDBACK", data_dir / "feedback.jsonl")
    monkeypatch.setattr(store, "LEXICON", data_dir / "lexicon.jsonl")

    monkeypatch.setattr(pipeline, "ARTIFACTS_DIR", artifacts)
    monkeypatch.setattr(pipeline, "CURRENT_POINTER", artifacts / "current.txt")
    monkeypatch.setattr(pipeline, "MODELS_DIR", models_dir)

    return {
        "data": data_dir,
        "models": models_dir,
        "artifacts": artifacts,
        "utterances": data_dir / "utterances.jsonl",
        "feedback": data_dir / "feedback.jsonl",
        "lexicon": data_dir / "lexicon.jsonl",
    }


@pytest.fixture()
def sample_pairs(isolated_data: dict[str, Path]) -> dict[str, Path]:
    """Ghi 3 cặp (raw -> corrected) mẫu vào utterances + feedback."""
    from lexicon import store

    items = [
        ("mở quảng chạy ra", "mở Qdrant ra"),
        ("dùng sherpa ông nx đi", "dùng sherpa-onnx đi"),
        ("mở quảng chạy giúp anh", "mở Qdrant giúp anh"),
    ]
    for idx, (raw, corrected) in enumerate(items):
        uid = f"u{idx}"
        store.append_jsonl(
            isolated_data["utterances"],
            store.Utterance(
                id=uid,
                ts="2026-10-31T23:07:00+0700",
                raw_text=raw,
                final_text=raw,
                model="zipformer-vi-68M",
                lane="interactive",
                duration_sec=2.0,
            ).to_json(),
        )
        store.append_jsonl(
            isolated_data["feedback"],
            store.Feedback(
                utterance_id=uid,
                ts="2026-10-31T23:08:00+0700",
                corrected_text=corrected,
                source="test",
            ).to_json(),
        )
    return isolated_data
