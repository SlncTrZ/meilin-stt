"""meilin-stt HTTP/WebSocket API.

Wing: code | Topic: stt-api | Updated: 2026-10-31 23:07

Bề mặt API (chi tiết: server/protocol.md):
  GET  /health                 trạng thái model + hotwords + device
  GET  /v1/models              danh sách model + trạng thái tải
  POST /v1/transcribe          transcribe file (multipart) — đường batch
  WS   /ws                     stream PCM16LE 16kHz — đường interactive
  POST /v1/feedback            người dùng sửa text -> dữ liệu cho nightly job
  POST /v1/admin/reload        (loopback) nạp lại hotwords/replacements
  GET  /v1/admin/status        (loopback) chi tiết vận hành

AUTH: KHÔNG nằm trong service này — do lớp ingress/agents phía trước lo.
Service chỉ fail-closed: bind ngoài loopback mà thiếu STT_ALLOW_PUBLIC=1 -> thoát.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lexicon import correct, store
from server import engine, vad
from server.engine import Settings

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meilin-stt.api")

SETTINGS = Settings()
_STARTED_AT = time.monotonic()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _bootstrap()
    yield


app = FastAPI(
    title="meilin-stt",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)
transcriber = engine.Transcriber(SETTINGS)
_rules: list[correct.Rule] = correct.load_rules()
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# --- startup ----------------------------------------------------------------
def _bootstrap() -> None:
    """Kiểm tra fail-closed + nạp replacement rules + đăng ký SIGHUP."""
    global _rules
    if SETTINGS.host not in _LOOPBACK and not SETTINGS.allow_public:
        raise RuntimeError(
            f"STT_HOST={SETTINGS.host} nằm ngoài loopback nhưng thiếu STT_ALLOW_PUBLIC=1. "
            "Đặt service sau ingress/Cloudflare Tunnel rồi bật cờ này nếu thật sự cần."
        )
    _rules = correct.load_rules()
    _install_sighup()
    log.info("meilin-stt khởi động: %s", json.dumps(engine.settings_summary(SETTINGS), ensure_ascii=False))


def _install_sighup() -> None:
    """SIGHUP = reload hotwords/replacements (systemd ExecReload)."""
    import signal
    import threading

    if not hasattr(signal, "SIGHUP"):  # Windows không có SIGHUP
        return

    def _handler(_signum: int, _frame: object) -> None:
        log.info("Nhận SIGHUP — nạp lại hotwords")
        global _rules
        threading.Thread(target=transcriber.reload, daemon=True).start()
        _rules = correct.load_rules()

    try:
        signal.signal(signal.SIGHUP, _handler)
    except (ValueError, OSError):  # không phải main thread / không hỗ trợ
        log.debug("Không đăng ký được SIGHUP")


# --- helpers ----------------------------------------------------------------
def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in _LOOPBACK


def _require_loopback(request: Request) -> None:
    if SETTINGS.admin_loopback_only and not _is_loopback(request):
        raise HTTPException(status_code=403, detail="admin endpoint chỉ cho loopback")


def _postprocess(text: str) -> tuple[str, list[dict[str, str]]]:
    """Áp bảng sửa lỗi lên bản final (không đụng partial)."""
    if not SETTINGS.auto_correct or not text or not _rules:
        return text, []
    fixed, applied = correct.apply_rules(text, _rules)
    fuzzy_rules = [r for r in _rules if r.fuzzy]
    if fuzzy_rules:
        fixed, fuzzy_applied = correct.fuzzy_apply(fixed, [r.right for r in fuzzy_rules])
        applied.extend(fuzzy_applied)
    return fixed, applied


def _decode_audio_upload(path: Path, target_sr: int = 16000) -> np.ndarray:
    """Đọc file audio -> float32 mono 16kHz. wav/flac trực tiếp, còn lại qua ffmpeg."""
    try:
        import soundfile as sf

        audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return engine.resample(audio, sr, target_sr) if sr != target_sr else audio
    except Exception as direct_err:  # noqa: BLE001 - thử ffmpeg
        exe = shutil.which(SETTINGS.ffmpeg_path)
        if not exe:
            raise HTTPException(
                status_code=415,
                detail=f"Không đọc được audio trực tiếp ({direct_err}) và không có ffmpeg",
            ) from direct_err
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            proc = subprocess.run(
                [exe, "-y", "-i", str(path), "-ar", str(target_sr), "-ac", "1", "-f", "wav", str(wav_path)],
                capture_output=True,
                timeout=300,
                check=False,
            )
            if proc.returncode != 0:
                raise HTTPException(status_code=415, detail="ffmpeg không decode được file")
            import soundfile as sf

            audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            return audio
        finally:
            wav_path.unlink(missing_ok=True)


# --- HTTP -------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    hotwords = Path(transcriber.hotwords_path) if transcriber.hotwords_path else None
    gpu = engine.gpu_state()
    return {
        "status": "ok",
        "model": SETTINGS.model_cfg["label"],
        "model_ready": engine.model_ready(SETTINGS),
        "model_dir": str(SETTINGS.model_dir),
        "hotwords_path": str(hotwords) if hotwords else "",
        "hotwords_terms": _count_terms(hotwords) if hotwords else 0,
        "replacement_rules": len(_rules),
        "providers": {
            "interactive": transcriber.provider_of("interactive"),
            "batch": transcriber.provider_of("batch"),
        },
        "gpu": gpu or "none",
        "partial_enabled": SETTINGS.partial_enabled,
        "uptime_sec": round(time.monotonic() - _STARTED_AT, 1),
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "active": SETTINGS.model_version,
        "available": {
            name: {
                "label": cfg["label"],
                "hf_repo": cfg["hf_repo"],
                "approx_ram_mb": cfg["ram_mb"],
            }
            for name, cfg in engine.MODEL_REGISTRY.items()
        },
    }


@app.post("/v1/transcribe")
async def transcribe_file(
    file: UploadFile = File(...),
    apply_corrections: bool = Query(default=True),
    segment: bool = Query(default=True),
) -> JSONResponse:
    """Transcribe 1 file trên đường batch (ưu tiên throughput)."""
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        shutil.copyfileobj(file.file, tmp)
    try:
        audio = _decode_audio_upload(tmp_path)
        started = time.monotonic()
        pieces: list[str] = []
        segments_meta: list[dict[str, Any]] = []
        if segment:
            segs = vad.segment_energy(audio)
        else:
            segs = [vad.Segment(0.0, len(audio) / 16000.0)]
        for seg in segs:
            chunk = vad.slice_audio(audio, seg)
            if chunk.size < 1600:  # < 0.1s: bỏ
                continue
            text = transcriber.transcribe_samples(chunk, 16000, lane="batch")
            if text:
                pieces.append(text)
                segments_meta.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
        text = " ".join(pieces).strip()
        if apply_corrections:
            text, _ = _postprocess(text)
        elapsed = time.monotonic() - started
        duration = len(audio) / 16000.0
        return JSONResponse(
            {
                "text": text,
                "duration_sec": round(duration, 2),
                "elapsed_sec": round(elapsed, 3),
                "rtf": round(elapsed / duration, 4) if duration > 0 else None,
                "provider": transcriber.provider_of("batch"),
                "segments": segments_meta,
            }
        )
    finally:
        tmp_path.unlink(missing_ok=True)


class FeedbackBody(BaseModel):
    """Người dùng sửa lại text — nguồn học chính của nightly job."""

    utterance_id: str = Field(default="", description="id trả về từ /ws final")
    corrected_text: str = Field(min_length=1)
    raw_text: str = Field(default="")
    source: str = Field(default="api")


@app.post("/v1/feedback")
async def feedback(body: FeedbackBody) -> dict[str, Any]:
    utterance_id = body.utterance_id or store.new_id()
    store.append_jsonl(
        store.FEEDBACK,
        store.Feedback(
            utterance_id=utterance_id,
            ts=store.now_ts(),
            corrected_text=body.corrected_text,
            source=body.source,
            raw_text=body.raw_text,
        ).to_json(),
    )
    return {"ok": True, "utterance_id": utterance_id}


@app.post("/v1/admin/reload")
async def admin_reload(request: Request) -> dict[str, Any]:
    _require_loopback(request)
    global _rules
    result = transcriber.reload()
    _rules = correct.load_rules()
    result["replacement_rules"] = len(_rules)
    log.info("Admin reload: %s", result)
    return result


@app.get("/v1/admin/status")
async def admin_status(request: Request) -> dict[str, Any]:
    _require_loopback(request)
    return {
        "settings": engine.settings_summary(SETTINGS),
        "providers": {
            "interactive": transcriber.provider_of("interactive"),
            "batch": transcriber.provider_of("batch"),
        },
        "hotwords_path": transcriber.hotwords_path,
        "replacement_rules": len(_rules),
        "gpu": engine.gpu_state() or "none",
        "data_dir": str(store.DATA_DIR),
        "utterances": len(store.read_jsonl(store.UTTERANCES)),
        "feedback": len(store.read_jsonl(store.FEEDBACK)),
    }


# --- WebSocket --------------------------------------------------------------
@app.websocket("/ws")
async def ws_stream(websocket: WebSocket) -> None:
    """Một connection = một utterance (client tự VAD, giống extension Pi)."""
    await websocket.accept()
    lane = websocket.query_params.get("lane", "interactive")
    sample_rate = int(websocket.query_params.get("sample_rate", "16000") or 16000)
    buffer = bytearray()
    last_partial_at = 0.0
    decode_lock = asyncio.Lock()
    await websocket.send_json({"type": "ready", "lane": lane, "sample_rate": 16000})

    async def finalize() -> None:
        nonlocal buffer, last_partial_at
        pcm = bytes(buffer)
        buffer = bytearray()
        last_partial_at = 0.0
        if len(pcm) < 3200:  # < 0.1s
            await websocket.send_json({"final": "", "utterance_id": store.new_id(), "corrections": []})
            return
        try:
            raw = await asyncio.to_thread(transcriber.transcribe_pcm16, pcm, sample_rate, lane)
        except Exception as exc:  # noqa: BLE001 - trả lỗi cho client, không sập
            log.exception("transcribe lỗi")
            await websocket.send_json({"error": str(exc)})
            return
        final_text, applied = _postprocess(raw)
        utterance_id = store.new_id()
        store.append_jsonl(
            store.UTTERANCES,
            store.Utterance(
                id=utterance_id,
                ts=store.now_ts(),
                raw_text=raw,
                final_text=final_text,
                model=SETTINGS.model_cfg["label"],
                lane=lane,
                duration_sec=round(len(pcm) / 2 / sample_rate, 2),
                corrections=applied,
            ).to_json(),
        )
        await websocket.send_json(
            {"final": final_text, "utterance_id": utterance_id, "corrections": applied, "raw": raw}
        )

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data:
                buffer.extend(data)
                max_bytes = int(SETTINGS.max_utterance_sec * sample_rate * 2)
                if len(buffer) >= max_bytes:
                    await finalize()
                    continue
                if (
                    SETTINGS.partial_enabled
                    and len(buffer) >= int(0.8 * sample_rate) * 2
                    and (time.monotonic() - last_partial_at) * 1000 >= SETTINGS.partial_interval_ms
                ):
                    last_partial_at = time.monotonic()
                    snapshot = bytes(buffer)
                    if not decode_lock.locked():
                        async with decode_lock:
                            try:
                                partial = await asyncio.to_thread(
                                    transcriber.transcribe_pcm16, snapshot, sample_rate, lane
                                )
                                await websocket.send_json({"partial": partial})
                            except Exception as exc:  # noqa: BLE001 - partial lỗi không quan trọng
                                log.debug("partial lỗi: %s", exc)
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                await websocket.send_json({"error": "invalid json"})
                continue
            kind = payload.get("type")
            if kind == "end":
                await finalize()
            elif kind == "reset":
                buffer = bytearray()
                last_partial_at = 0.0
                await websocket.send_json({"type": "reset_ok"})
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"error": f"unknown type: {kind}"})
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


def _count_terms(path: Path) -> int:
    try:
        return sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    except OSError:
        return 0


def main() -> None:
    import uvicorn

    uvicorn.run(
        "server.app:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        log_level="info",
        ws_max_size=16 * 1024 * 1024,
    )


if __name__ == "__main__":
    main()
