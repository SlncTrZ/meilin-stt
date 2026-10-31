"""Integration test — cần model thật (260MB), nên mặc định SKIP.

Wing: code | Topic: stt-tests-integration | Updated: 2026-10-31 23:35

Chạy trên máy thật:
    STT_RUN_INTEGRATION=1 python -m pytest -m integration -q

Vì sao tách riêng: CI không tải model (không phụ thuộc HuggingFace, không tốn 260MB
mỗi lần chạy). Nhưng bỏ hẳn thì mất lớp kiểm tra "model + hotwords + WS có thật sự
chạy" — nên để đây và chạy tay sau mỗi lần đổi engine/protocol.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from server import engine
from server.engine import Settings

pytestmark = pytest.mark.integration

RUN = os.environ.get("STT_RUN_INTEGRATION") == "1"
MODEL_READY = engine.model_ready(Settings())

pytestmark = [pytestmark, pytest.mark.skipif(not RUN or not MODEL_READY, reason="cần STT_RUN_INTEGRATION=1 và model đã tải")]


def _pcm(seconds: float = 1.0, freq: float = 220.0) -> bytes:
    t = np.arange(int(seconds * 16000)) / 16000.0
    audio = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return (audio * 32767).astype(np.int16).tobytes()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from server.app import app

    return TestClient(app)


def test_health_reports_loaded_model(client) -> None:
    payload = client.get("/health").json()
    assert payload["model_ready"] is True
    assert payload["hotwords_terms"] >= 0


def test_ws_returns_final_with_utterance_id(client) -> None:
    with client.websocket_connect("/ws?lane=interactive") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(_pcm(0.5))
        ws.send_json({"type": "end"})
        message = ws.receive_json()
        while "final" not in message and "error" not in message:
            message = ws.receive_json()
        assert "final" in message
        assert message["utterance_id"], "cần id để gửi feedback"
        assert "raw" in message


def test_transcribe_endpoint_returns_rtf(client) -> None:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(_pcm(1.0))
    buf.seek(0)
    resp = client.post("/v1/transcribe", files={"file": ("t.wav", buf, "audio/wav")})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["duration_sec"] > 0
    assert payload["rtf"] is not None
