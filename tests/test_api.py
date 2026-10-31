"""Test VAD năng lượng + API surface (không cần model thật).

Wing: code | Topic: stt-tests-api | Updated: 2026-10-31 23:07
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from server import vad


def _tone(seconds: float, freq: float = 220.0, amp: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000.0
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_segment_energy_finds_single_speech_block() -> None:
    audio = np.concatenate([np.zeros(int(0.5 * 16000), np.float32), _tone(1.0), np.zeros(int(0.8 * 16000), np.float32)])
    segments = vad.segment_energy(audio)
    assert len(segments) == 1, f"phải gộp 1 đoạn, nhận {segments}"
    assert 0.3 < segments[0].duration < 1.6


def test_segment_energy_silence_returns_empty() -> None:
    assert vad.segment_energy(np.zeros(int(2 * 16000), np.float32)) == []


def test_segment_energy_splits_on_long_silence() -> None:
    audio = np.concatenate([_tone(0.6), np.zeros(int(1.0 * 16000), np.float32), _tone(0.6)])
    assert len(vad.segment_energy(audio)) == 2


def test_segment_energy_caps_long_segments() -> None:
    audio = _tone(70.0)
    segments = vad.segment_energy(audio, max_segment=30.0)
    assert all(seg.duration <= 30.0 + 1e-6 for seg in segments)
    assert sum(seg.duration for seg in segments) > 60.0


def test_slice_audio_is_bounded() -> None:
    audio = _tone(1.0)
    seg = vad.Segment(0.5, 0.75)
    chunk = vad.slice_audio(audio, seg)
    assert chunk.size == int(0.25 * 16000)


def test_segments_from_probs_thresholds() -> None:
    probs = np.array([0.1, 0.9, 0.9, 0.1, 0.9, 0.9, 0.9, 0.1], dtype=np.float32)
    segs = vad.segments_from_probs(probs, hop_sec=0.1, threshold=0.5)
    assert len(segs) == 2


# --- API --------------------------------------------------------------------
@pytest.fixture()
def client():
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from server.app import app

    return fastapi_testclient.TestClient(app)


def test_health_reports_model_and_lanes(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert "interactive" in payload["providers"]
    assert payload["partial_enabled"] in (True, False)


def test_admin_status_blocked_from_non_loopback(client) -> None:
    """Bất biến: admin endpoint không lộ ra ngoài loopback (auth do lớp khác lo)."""
    resp = client.get("/v1/admin/status")
    assert resp.status_code == 403


def test_models_endpoint_lists_registry(client) -> None:
    payload = client.get("/v1/models").json()
    assert payload["active"]
    assert set(payload["available"]) >= {"68m", "30m"}


def test_feedback_roundtrip(client, isolated_data: dict[str, Path]) -> None:
    from lexicon import store

    resp = client.post("/v1/feedback", json={"corrected_text": "mở Qdrant ra", "source": "test"})
    assert resp.status_code == 200
    uid = resp.json()["utterance_id"]
    rows = store.read_jsonl(isolated_data["feedback"])
    assert rows[-1]["utterance_id"] == uid
    assert rows[-1]["corrected_text"] == "mở Qdrant ra"
