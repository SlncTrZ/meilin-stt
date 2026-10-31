#!/usr/bin/env python3
"""
Sherpa-ONNX WebSocket ASR Worker — Local speech recognition cho Pi voice-input.

Chay WebSocket server, nhan PCM16 audio stream, transcribe bang sherpa-onnx.
Mac dinh dung model zipformer-vi-68M (Tieng Viet, ~70k gio, ~260MB, chay CPU)
— chinh xac hon nhieu so voi model 30M cu. Hoi tro ca hai:
  - 68M: csukuangfj/sherpa-onnx-zipformer-vi-2025-04-20 (default)
  - 30M: hynt/Zipformer-30M-RNNT-6000h (cu, chinh xac thap hon)

Protocol tuong thich voice-input extension websocket mode:
  - GET /health -> {"status":"ok", "model_loaded":true}
  - WebSocket ws://host:port/ws
    - Client -> Server: binary PCM16 S16LE 16kHz mono
    - Client -> Server: {"type":"end"} JSON khi het cau
    - Server -> Client: {"final":"transcribed text"}

Usage:
  python sherpa_worker.py

Environment:
  SHERPA_MODEL_VERSION version: "68m" (default) | "30m"
  SHERPA_MODEL_DIR     path to model files (default: ~/.cache/sherpa-onnx/zipformer-vi-68M)
  SHERPA_HOST          bind address (default: 127.0.0.1)
  SHERPA_PORT          port (default: 8766)
  SHERPA_NUM_THREADS   CPU threads (default: 4)

Wing: openclaw | Topic: voice | Updated: 2025-08-08
"""

import json
import logging
import os
import struct
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from sherpa_onnx.offline_recognizer import OfflineRecognizer

logging.basicConfig(
    level=logging.INFO,
    format="[sherpa-worker] %(levelname)s %(message)s",
)
log = logging.getLogger("sherpa-worker")

# --- Config ----------------------------------------------------------------
# Model registry: version -> (HF repo, ten file nguon -> ten file local)
MODEL_REGISTRY = {
    "68m": {
        "hf_repo": "csukuangfj/sherpa-onnx-zipformer-vi-2025-04-20",
        "dir": "zipformer-vi-68M",
        "files": {
            "encoder-epoch-12-avg-8.onnx": "encoder.onnx",
            "decoder-epoch-12-avg-8.onnx": "decoder.onnx",
            "joiner-epoch-12-avg-8.onnx": "joiner.onnx",
            "bpe.model": "bpe.model",
            "tokens.txt": "tokens.txt",
        },
        "label": "zipformer-vi-68M",
    },
    "30m": {
        "hf_repo": "hynt/Zipformer-30M-RNNT-6000h",
        "dir": "zipformer-vi-30M",
        "files": {
            "encoder-epoch-20-avg-10.int8.onnx": "encoder.int8.onnx",
            "decoder-epoch-20-avg-10.onnx": "decoder.onnx",
            "joiner-epoch-20-avg-10.int8.onnx": "joiner.int8.onnx",
            "bpe.model": "bpe.model",
        },
        "label": "zipformer-vi-30M",
    },
}

# k2-fsa release asset (30M, co san tokens.txt day du) — fallback khi thieu tokens.txt
K2FSA_30M_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-zipformer-vi-30M-int8-2026-02-09.tar.bz2"
)

MODEL_VERSION = os.environ.get("SHERPA_MODEL_VERSION", "68m")
if MODEL_VERSION not in MODEL_REGISTRY:
    MODEL_VERSION = "68m"
_MODEL_CFG = MODEL_REGISTRY[MODEL_VERSION]

DEFAULT_MODEL_DIR = Path.home() / ".cache" / "sherpa-onnx" / _MODEL_CFG["dir"]
MODEL_DIR = Path(os.environ.get("SHERPA_MODEL_DIR", str(DEFAULT_MODEL_DIR)))
HOST = os.environ.get("SHERPA_HOST", "127.0.0.1")


def env_int(name: str, default: int) -> int:
    """Read int env var, fallback to default khi gia tri khong hop le."""
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


PORT = env_int("SHERPA_PORT", 8766)
NUM_THREADS = env_int("SHERPA_NUM_THREADS", 4)

recognizer: OfflineRecognizer | None = None


# --- Model -----------------------------------------------------------------


def _find_model_file(model_dir: Path, prefix: str, exts: tuple[str, ...]) -> str:
    """Tim file model theo prefix + duoi onnx (ho tro ten file linh hoat)."""
    for ext in exts:
        candidate = model_dir / f"{prefix}{ext}"
        if candidate.exists():
            return str(candidate)
    matches = sorted(model_dir.glob(f"{prefix}*.onnx"))
    if matches:
        return str(matches[0])
    return str(model_dir / f"{prefix}{exts[0]}")


def create_recognizer(model_dir: Path) -> OfflineRecognizer:
    """Initialize sherpa-onnx offline recognizer from model files."""
    encoder = _find_model_file(model_dir, "encoder", (".int8.onnx", ".onnx"))
    decoder = _find_model_file(model_dir, "decoder", (".onnx", ".int8.onnx"))
    joiner = _find_model_file(model_dir, "joiner", (".int8.onnx", ".onnx"))
    tokens = str(model_dir / "tokens.txt")
    bpe_vocab_path = model_dir / "bpe.model"

    missing = [f for f in [encoder, decoder, joiner, tokens] if not os.path.exists(f)]
    if missing:
        raise FileNotFoundError(
            f"Missing model files in {model_dir}: {missing}\n"
            f"Model: {_MODEL_CFG['hf_repo']} (set SHERPA_MODEL_VERSION=30m de dung model cu)"
        )

    log.info("Loading model from %s", model_dir)
    rec = OfflineRecognizer.from_transducer(
        encoder=encoder,
        decoder=decoder,
        joiner=joiner,
        tokens=tokens,
        num_threads=NUM_THREADS,
        provider="cpu",
        modeling_unit="bpe",
        bpe_vocab=str(bpe_vocab_path) if bpe_vocab_path.exists() else "",
        hotwords_file="",
        hotwords_score=1.5,
        decoding_method="greedy_search",
        max_active_paths=4,
        blank_penalty=0.0,
        rule_fsts="",
        rule_fars="",
        lm="",
        lm_scale=0.0,
        hr_dict_dir="",
        hr_rule_fsts="",
        hr_lexicon="",
        lodr_fst="",
        lodr_scale=0.0,
        debug=False,
        model_type="",
        sample_rate=16000,
        feature_dim=80,
        dither=0.0,
    )
    log.info("Model loaded: %s (%d threads)", _MODEL_CFG["label"], NUM_THREADS)
    return rec


def _extract_k2fsa_30m(model_dir: Path) -> None:
    """Download/extract k2-fsa 30M release asset (co tokens.txt day du)."""
    log.info("  Falling back to k2-fsa release asset (30M-int8)...")
    import tarfile

    import requests

    archive = model_dir / "model.tar.bz2"
    log.info("  Downloading %s", K2FSA_30M_URL)
    resp = requests.get(K2FSA_30M_URL, timeout=600)
    resp.raise_for_status()
    try:
        with open(archive, "wb") as f:
            f.write(resp.content)
    except OSError as exc:
        raise RuntimeError(f"Failed to write {archive}: {exc}") from exc
    log.info("    Done (%.1f MB)", len(resp.content) / (1024 * 1024))

    wanted = {
        "encoder.int8.onnx",
        "decoder.onnx",
        "joiner.int8.onnx",
        "tokens.txt",
        "bpe.model",
    }
    with tarfile.open(archive, "r:bz2") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = Path(member.name).name
            if name not in wanted:
                continue
            src = tar.extractfile(member)
            if src is None:
                continue
            try:
                with open(model_dir / name, "wb") as f:
                    f.write(src.read())
            except OSError as exc:
                raise RuntimeError(
                    f"Failed to write {model_dir / name}: {exc}"
                ) from exc
            log.info("  Extracted %s", name)
    archive.unlink(missing_ok=True)


async def download_model(model_dir: Path) -> None:
    """Download model from Hugging Face if not already present."""
    required = set(_MODEL_CFG["files"].values())
    have = {dst for dst in required if (model_dir / dst).exists()}
    missing = required - have
    # tokens.txt la bat buoc; neu thieu thi thu fallback k2-fsa (30M)
    if (model_dir / "tokens.txt").exists():
        missing.discard("tokens.txt")
    if not missing:
        return

    log.info("=" * 60)
    log.info("Downloading %s model...", _MODEL_CFG["label"])
    log.info("Source: https://huggingface.co/%s", _MODEL_CFG["hf_repo"])
    log.info("=" * 60)

    model_dir.mkdir(parents=True, exist_ok=True)
    import requests

    base = f"https://huggingface.co/{_MODEL_CFG['hf_repo']}/resolve/main"
    for src_name, dst_name in _MODEL_CFG["files"].items():
        if dst_name not in missing:
            continue
        url = f"{base}/{src_name}"
        dest = model_dir / dst_name
        log.info("  Downloading %s -> %s ...", src_name, dst_name)
        resp = requests.get(url, timeout=600)
        resp.raise_for_status()
        try:
            with open(dest, "wb") as f:
                f.write(resp.content)
        except OSError as exc:
            raise RuntimeError(f"Failed to write {dest}: {exc}") from exc
        mb = len(resp.content) / (1024 * 1024)
        log.info("    Done (%.1f MB)", mb)

    # 30M tu HF khong co tokens.txt -> fallback k2-fsa release asset
    if MODEL_VERSION == "30m" and not (model_dir / "tokens.txt").exists():
        _extract_k2fsa_30m(model_dir)

    log.info("Model download complete!")
    log.info("Model directory: %s", model_dir)


# --- Lifespan & HTTP Endpoints -------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global recognizer
    await download_model(MODEL_DIR)
    recognizer = create_recognizer(MODEL_DIR)
    yield
    recognizer = None


app = FastAPI(title="Sherpa-ONNX ASR Worker", version="2.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_loaded": recognizer is not None,
        "model": _MODEL_CFG["label"],
        "model_version": MODEL_VERSION,
        "host": HOST,
        "port": PORT,
    }


# --- WebSocket -----------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    log.info("WebSocket client connected")

    audio_buffer = bytearray()

    try:
        while True:
            data = await ws.receive()

            if data["type"] == "websocket.receive":
                raw = data.get("text") or data.get("bytes")
                if raw is None:
                    continue

                if isinstance(raw, bytes):
                    audio_buffer.extend(raw)

                elif isinstance(raw, str):
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        await ws.send_json({"error": "invalid JSON"})
                        continue

                    if msg.get("type") == "end":
                        if len(audio_buffer) < 64:
                            await ws.send_json({"final": ""})
                            audio_buffer.clear()
                            continue

                        text = transcribe(bytes(audio_buffer))
                        audio_buffer.clear()

                        if text:
                            await ws.send_json({"final": text})
                            log.info("Transcribed: %s", text[:120])
                        else:
                            await ws.send_json({"final": ""})

            elif data["type"] == "websocket.disconnect":
                log.info("WebSocket client disconnected")
                break

    except WebSocketDisconnect:
        log.info("WebSocket client disconnected")
    except Exception as e:
        log.error("WebSocket error: %s", e)
    finally:
        audio_buffer.clear()


# --- Transcription -------------------------------------------------------


def transcribe(pcm_data: bytes) -> str:
    """Transcribe PCM16 S16LE 16kHz mono audio using sherpa-onnx."""
    global recognizer
    if recognizer is None:
        return ""

    sample_count = len(pcm_data) // 2
    if sample_count == 0:
        return ""

    samples = [
        struct.unpack("<h", pcm_data[i : i + 2])[0] / 32768.0
        for i in range(0, len(pcm_data), 2)
    ]

    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_streams([stream])
    return stream.result.text.strip()


# --- Entry point ---------------------------------------------------------

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("Sherpa-ONNX ASR Worker")
    log.info("=" * 50)
    log.info("Model:   %s (%s)", _MODEL_CFG["label"], MODEL_VERSION)
    log.info("Dir:     %s", MODEL_DIR)
    log.info("Host:    %s", HOST)
    log.info("Port:    %d", PORT)
    log.info("Threads: %d", NUM_THREADS)
    log.info("=" * 50)
    log.info("Health endpoint:  http://%s:%d/health", HOST, PORT)
    log.info("WebSocket endpoint: ws://%s:%d/ws", HOST, PORT)
    log.info("=" * 50)

    uvicorn.run(app, host=HOST, port=PORT, log_level="info", ws_max_size=2**22)
