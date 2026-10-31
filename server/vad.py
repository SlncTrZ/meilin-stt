"""meilin-stt VAD — cắt audio dài thành đoạn có tiếng nói.

Wing: code | Topic: stt-vad | Updated: 2026-10-31 23:07

Hai lựa chọn:
  - `segment_energy`: VAD theo năng lượng (RMS) — không cần model, deterministic,
    đã có unit test. Dùng cho `/v1/transcribe` khi client không cắt sẵn.
  - `SileroVad`: VAD ONNX của Silero — chính xác hơn ở audio nhiễu, nhưng cần
    file `silero_vad.onnx` và **chưa được verify trên máy đích** (xem
    docs/benchmark.md). Mặc định TẮT; bật bằng `STT_VAD=silero`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("meilin-stt.vad")


@dataclass(frozen=True)
class Segment:
    """Một đoạn có tiếng nói, đơn vị giây."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _frame_rms(audio: np.ndarray, frame: int, hop: int) -> np.ndarray:
    if audio.size < frame:
        return np.array([], dtype=np.float32)
    n = 1 + (audio.size - frame) // hop
    out = np.empty(n, dtype=np.float32)
    for i in range(n):
        chunk = audio[i * hop : i * hop + frame]
        out[i] = float(np.sqrt(np.mean(chunk * chunk) + 1e-12))
    return out


def segment_energy(
    audio: np.ndarray,
    sample_rate: int = 16000,
    *,
    threshold: float = 0.01,
    min_speech: float = 0.30,
    min_silence: float = 0.50,
    pad: float = 0.10,
    max_segment: float = 30.0,
) -> list[Segment]:
    """Cắt theo RMS. Trả [] nếu không có tiếng nói (im lặng toàn bộ)."""
    if audio.size == 0:
        return []
    frame = int(0.032 * sample_rate)  # 32ms
    hop = int(0.016 * sample_rate)  # 16ms
    rms = _frame_rms(audio, frame, hop)
    if rms.size == 0:
        return []
    hop_sec = hop / float(sample_rate)
    voiced = rms > threshold

    segments: list[Segment] = []
    start_idx: int | None = None
    last_voiced_idx = 0
    silence_run = 0.0
    for idx, is_voiced in enumerate(voiced):
        if is_voiced:
            if start_idx is None:
                start_idx = idx
            last_voiced_idx = idx
            silence_run = 0.0
            continue
        if start_idx is None:
            continue
        silence_run += hop_sec
        if silence_run >= min_silence:
            # Cắt tại frame có tiếng nói CUỐI CÙNG, không phải frame phát hiện im lặng
            # (nếu không, mỗi segment bị kéo dài thêm đúng min_silence).
            _append(
                segments,
                start_idx,
                last_voiced_idx,
                hop_sec,
                frame,
                sample_rate,
                pad,
                min_speech,
                max_segment,
            )
            start_idx = None
            silence_run = 0.0
    if start_idx is not None:
        _append(
            segments,
            start_idx,
            last_voiced_idx,
            hop_sec,
            frame,
            sample_rate,
            pad,
            min_speech,
            max_segment,
        )
    return segments


def _append(
    out: list[Segment],
    start_idx: int,
    end_idx: int,
    hop_sec: float,
    frame: int,
    sample_rate: int,
    pad: float,
    min_speech: float,
    max_segment: float,
) -> None:
    start = max(0.0, start_idx * hop_sec - pad)
    end = (end_idx * hop_sec) + frame / float(sample_rate) + pad
    if end - start < min_speech:
        return
    while end - start > max_segment:
        out.append(Segment(start, start + max_segment))
        start += max_segment
    out.append(Segment(start, end))


def slice_audio(audio: np.ndarray, seg: Segment, sample_rate: int = 16000) -> np.ndarray:
    a = int(seg.start * sample_rate)
    b = int(seg.end * sample_rate)
    return audio[max(0, a) : min(audio.size, b)]


class SileroVad:
    """Wrapper Silero VAD ONNX (v5). CHƯA VERIFY trên máy đích — xem docs."""

    def __init__(self, model_path: Path | str, sample_rate: int = 16000):
        import onnxruntime as ort  # import cục bộ: chỉ cần khi bật VAD=Silero

        self.sample_rate = sample_rate
        self.session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)
        log.info("Silero VAD nạp từ %s", model_path)

    def speech_prob(self, chunk: np.ndarray) -> float:
        """Xác suất có tiếng nói cho chunk 512 mẫu (32ms @16kHz)."""
        audio = np.concatenate([self._context[0], chunk.astype(np.float32)])
        inputs = {
            "input": audio.reshape(1, -1),
            "state": self._state,
            "sr": np.array(self.sample_rate, dtype=np.int64),
        }
        out, state = self.session.run(None, inputs)
        self._state = state
        self._context = audio[-64:].reshape(1, 64)
        return float(np.asarray(out).reshape(-1)[0])


def segments_from_probs(probs: np.ndarray, hop_sec: float, threshold: float = 0.5) -> list[Segment]:
    """Chuyển chuỗi xác suất thành segment (dùng chung cho Silero)."""
    if probs.size == 0:
        return []
    out: list[Segment] = []
    start: int | None = None
    for idx, p in enumerate(probs):
        if p >= threshold and start is None:
            start = idx
        elif p < threshold and start is not None:
            out.append(Segment(start * hop_sec, idx * hop_sec))
            start = None
    if start is not None:
        out.append(Segment(start * hop_sec, probs.size * hop_sec))
    return [s for s in out if s.duration >= 0.20]
