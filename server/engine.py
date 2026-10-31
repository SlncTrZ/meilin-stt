"""meilin-stt engine — model registry, recognizer lifecycle, device profile.

Wing: code | Topic: stt-engine | Updated: 2026-10-31 23:07

Nguyên tắc thiết kế:
  - Hai đường suy luận độc lập: "interactive" (mic, độ trễ ưu tiên) và "batch"
    (file dài, throughput ưu tiên). Mỗi đường chọn thiết bị riêng theo
    `data/device_profile.json` — số đo thật, không đoán.
  - GPU là tài nguyên của dịch vụ khác: mặc định KHÔNG chạm GPU. Mọi lần chạm
    GPU đều qua `allow_gpu()` (kiểm headroom) và có fallback CPU.
  - Hotwords là inference-time biasing (không phải training): nạp từ
    `models/hotwords.txt`, đường dẫn có thể trỏ tới artifact của nightly job.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import threading
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("meilin-stt.engine")

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"
DATA_DIR = BASE_DIR / "data"

# --- Model registry ---------------------------------------------------------
# version -> HF repo + tên file nguồn -> tên file local.
# 68m là mặc định: cùng model với extension Pi đang dùng (chất lượng cao hơn 30M).
MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "68m": {
        "hf_repo": "csukuangfj/sherpa-onnx-zipformer-vi-2025-04-20",
        "dir": "zipformer-vi-68M",
        "label": "zipformer-vi-68M",
        "ram_mb": 900,
        "files": {
            "encoder-epoch-12-avg-8.onnx": "encoder.onnx",
            "decoder-epoch-12-avg-8.onnx": "decoder.onnx",
            "joiner-epoch-12-avg-8.onnx": "joiner.onnx",
            "bpe.model": "bpe.model",
            "tokens.txt": "tokens.txt",
        },
    },
    "30m": {
        "hf_repo": "hynt/Zipformer-30M-RNNT-6000h",
        "dir": "zipformer-vi-30M",
        "label": "zipformer-vi-30M",
        "ram_mb": 220,
        "files": {
            "encoder-epoch-20-avg-10.int8.onnx": "encoder.int8.onnx",
            "decoder-epoch-20-avg-10.onnx": "decoder.onnx",
            "joiner-epoch-20-avg-10.int8.onnx": "joiner.int8.onnx",
            "bpe.model": "bpe.model",
        },
    },
}


def env_int(name: str, default: int) -> int:
    """Đọc int từ env, lỗi -> default (không raise khi khởi động)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("env %s=%r không phải int, dùng %s", name, raw, default)
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("env %s=%r không phải float, dùng %s", name, raw, default)
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Cấu hình bất biến đọc 1 lần khi import."""

    host: str = os.environ.get("STT_HOST", "127.0.0.1")
    port: int = env_int("STT_PORT", 8766)
    model_version: str = os.environ.get("STT_MODEL", "68m")
    model_dir_override: str = os.environ.get("STT_MODEL_DIR", "")
    num_threads: int = env_int("STT_NUM_THREADS", 4)
    device_interactive: str = os.environ.get("STT_DEVICE_INTERACTIVE", "cpu")
    device_batch: str = os.environ.get("STT_DEVICE_BATCH", "cpu")
    partial_enabled: bool = env_bool("STT_PARTIAL", True)
    partial_interval_ms: int = env_int("STT_PARTIAL_INTERVAL_MS", 500)
    max_utterance_sec: float = env_float("STT_MAX_UTTERANCE_SEC", 60.0)
    hotwords_score: float = env_float("STT_HOTWORDS_SCORE", 1.5)
    auto_correct: bool = env_bool("STT_AUTO_CORRECT", True)
    allow_public: bool = env_bool("STT_ALLOW_PUBLIC", False)
    admin_loopback_only: bool = env_bool("STT_ADMIN_LOOPBACK_ONLY", True)
    gpu_min_free_mb: int = env_int("STT_GPU_MIN_FREE_MB", 1500)
    gpu_max_util: int = env_int("STT_GPU_MAX_UTIL", 40)
    ffmpeg_path: str = os.environ.get("STT_FFMPEG", "ffmpeg")
    max_upload_mb: int = env_int("STT_MAX_UPLOAD_MB", 200)

    @property
    def model_cfg(self) -> dict[str, Any]:
        return MODEL_REGISTRY.get(self.model_version, MODEL_REGISTRY["68m"])

    @property
    def model_dir(self) -> Path:
        if self.model_dir_override:
            return Path(self.model_dir_override).expanduser()
        return Path.home() / ".cache" / "sherpa-onnx" / self.model_cfg["dir"]


# --- Device profile ---------------------------------------------------------
DEVICE_PROFILE_PATH = DATA_DIR / "device_profile.json"


def load_device_profile() -> dict[str, Any]:
    """Đọc profile do `meilin-stt bench --apply` ghi. Thiếu file -> rỗng."""
    try:
        return json.loads(DEVICE_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_device_profile(profile: dict[str, Any]) -> Path:
    DEVICE_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEVICE_PROFILE_PATH.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return DEVICE_PROFILE_PATH


# --- GPU headroom gate ------------------------------------------------------
def gpu_state() -> dict[str, Any] | None:
    """Đọc trạng thái GPU qua nvidia-smi. None nếu không có GPU/nvidia-smi."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.free,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return None
    try:
        return {
            "name": parts[0],
            "free_mb": int(float(parts[1])),
            "total_mb": int(float(parts[2])),
            "util_pct": int(float(parts[3])),
        }
    except ValueError:
        return None


def allow_gpu(settings: Settings) -> tuple[bool, str]:
    """GPU chỉ được dùng khi thật sự còn chỗ. Trả (ok, lý do)."""
    state = gpu_state()
    if state is None:
        return False, "nvidia-smi không có sẵn"
    if state["free_mb"] < settings.gpu_min_free_mb:
        return False, f"VRAM trống {state['free_mb']}MB < {settings.gpu_min_free_mb}MB"
    if state["util_pct"] > settings.gpu_max_util:
        return False, f"GPU util {state['util_pct']}% > {settings.gpu_max_util}%"
    return True, f"GPU rảnh ({state['free_mb']}MB, util {state['util_pct']}%)"


# --- Model files ------------------------------------------------------------
def _find_model_file(model_dir: Path, prefix: str, exts: tuple[str, ...]) -> str:
    for ext in exts:
        candidate = model_dir / f"{prefix}{ext}"
        if candidate.exists():
            return str(candidate)
    matches = sorted(model_dir.glob(f"{prefix}*.onnx"))
    if matches:
        return str(matches[0])
    return str(model_dir / f"{prefix}{exts[0]}")


def model_ready(settings: Settings) -> bool:
    model_dir = settings.model_dir
    if not (model_dir / "tokens.txt").exists():
        return False
    return bool(sorted(model_dir.glob("encoder*.onnx")))


def download_model(settings: Settings, version: str | None = None) -> Path:
    """Tải model từ HuggingFace (idempotent). Trả về thư mục model."""
    version = version or settings.model_version
    cfg = MODEL_REGISTRY.get(version, MODEL_REGISTRY["68m"])
    model_dir = (
        Path(settings.model_dir_override).expanduser()
        if settings.model_dir_override
        else Path.home() / ".cache" / "sherpa-onnx" / cfg["dir"]
    )
    model_dir.mkdir(parents=True, exist_ok=True)
    base = f"https://huggingface.co/{cfg['hf_repo']}/resolve/main"
    for src_name, dst_name in cfg["files"].items():
        dest = model_dir / dst_name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        url = f"{base}/{src_name}"
        log.info("Tải %s -> %s", url, dest)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh, length=1024 * 512)
        tmp.replace(dest)
    _extract_legacy_30m(model_dir)
    return model_dir


def _extract_legacy_30m(model_dir: Path) -> None:
    """Model 30M cũ thiếu tokens.txt: lấy từ release tar.bz2 của k2-fsa."""
    if (model_dir / "tokens.txt").exists():
        return
    archive = model_dir / "model.tar.bz2"
    if not archive.exists():
        url = (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-zipformer-vi-30M-int8-2026-02-09.tar.bz2"
        )
        log.info("30M thiếu tokens.txt, tải %s", url)
        try:
            urllib.request.urlretrieve(url, archive)
        except OSError as exc:
            log.warning("Không tải được archive 30M: %s", exc)
            return
    wanted = {"tokens.txt", "bpe.model"}
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                if name not in wanted or not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                (model_dir / name).write_bytes(extracted.read())
                log.info("Đã trích %s", name)
    except (tarfile.TarError, OSError) as exc:
        log.warning("Trích archive 30M thất bại: %s", exc)


# --- BPE vocab --------------------------------------------------------------
def _write_vocab(path: Path, lines: list[str]) -> None:
    """Ghi vocab text, mỗi dòng một token (kèm score)."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_bpe_vocab(model_dir: Path) -> str:
    """Trả đường dẫn file BPE vocab DẠNG TEXT (mỗi dòng `token score`).

    sherpa-onnx yêu cầu `bpe_vocab` là vocab text, KHÔNG phải `bpe.model` (binary
    sentencepiece). Đưa binary vào sẽ làm C++ layer exit ngay với
    "Each line in vocab should contain two items ...". Vì vậy ta sinh file text
    từ `bpe.model` một lần rồi cache.
    """
    spm_path = model_dir / "bpe.model"
    text_path = model_dir / "bpe.vocab"
    if text_path.exists() and text_path.stat().st_size > 0:
        return str(text_path)
    if not spm_path.exists():
        return ""
    try:
        import sentencepiece as spm  # type: ignore[import-not-found]

        processor = spm.SentencePieceProcessor(model_file=str(spm_path))
        lines = [
            f"{processor.id_to_piece(i)} {processor.get_score(i):.10g}"
            for i in range(processor.get_piece_size())
        ]
    except ImportError:
        log.warning("Không có sentencepiece — không sinh được bpe.vocab, hotwords sẽ tắt")
        return ""
    except Exception as exc:  # noqa: BLE001 - model lạ -> báo và tắt hotwords
        log.warning("Không đọc được bpe.model (%s)", exc)
        return ""

    target = text_path
    try:
        _write_vocab(target, lines)
    except OSError:
        # Model dir có thể read-only (Docker/hệ thống) -> cache vào data/
        fallback_dir = DATA_DIR / "cache"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        target = fallback_dir / f"bpe-{model_dir.name}.vocab"
        _write_vocab(target, lines)
    log.info("Sinh BPE vocab text: %s (%d token)", target, len(lines))
    return str(target)


# --- Recognizer -------------------------------------------------------------
def create_recognizer(
    model_dir: Path,
    settings: Settings,
    provider: str = "cpu",
    hotwords_file: str = "",
    hotwords_score: float = 1.5,
):
    """Tạo OfflineRecognizer. raise FileNotFoundError nếu thiếu file model."""
    from sherpa_onnx.offline_recognizer import OfflineRecognizer

    encoder = _find_model_file(model_dir, "encoder", (".int8.onnx", ".onnx"))
    decoder = _find_model_file(model_dir, "decoder", (".onnx", ".int8.onnx"))
    joiner = _find_model_file(model_dir, "joiner", (".int8.onnx", ".onnx"))
    tokens = str(model_dir / "tokens.txt")
    bpe_vocab_text = ensure_bpe_vocab(model_dir) if hotwords_file else ""

    missing = [f for f in (encoder, decoder, joiner, tokens) if not Path(f).exists()]
    if missing:
        raise FileNotFoundError(
            f"Thiếu file model trong {model_dir}: {missing}. "
            f"Chạy: python scripts/download-models.py --model {settings.model_version}"
        )

    kwargs: dict[str, Any] = {
        "encoder": encoder,
        "decoder": decoder,
        "joiner": joiner,
        "tokens": tokens,
        "num_threads": settings.num_threads,
        "provider": provider,
        "modeling_unit": "bpe" if bpe_vocab_text else "cjkchar",
        "bpe_vocab": bpe_vocab_text,
        "hotwords_file": hotwords_file,
        "hotwords_score": hotwords_score,
        "decoding_method": "modified_beam_search" if hotwords_file else "greedy_search",
        "max_active_paths": 4,
        "sample_rate": 16000,
        "feature_dim": 80,
        "dither": 0.0,
    }
    log.info(
        "Nạp recognizer provider=%s threads=%d hotwords=%s score=%.2f",
        provider,
        settings.num_threads,
        bool(hotwords_file),
        hotwords_score,
    )
    return OfflineRecognizer.from_transducer(**kwargs)


class Transcriber:
    """Giữ recognizer theo từng đường (interactive/batch), nạp lười, reload được."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._recognizers: dict[str, Any] = {}
        self._providers: dict[str, str] = {}
        self.hotwords_path = self._resolve_hotwords_path()

    # -- hotwords ----------------------------------------------------------
    def _resolve_hotwords_path(self) -> str:
        """Ưu tiên artifact nightly (`current`), fallback models/hotwords.txt."""
        current = DATA_DIR / "artifacts" / "current.txt"
        if current.exists():
            try:
                stamp = current.read_text(encoding="utf-8").strip()
                candidate = DATA_DIR / "artifacts" / stamp / "hotwords.txt"
                if candidate.exists() and candidate.stat().st_size > 0:
                    log.info("Hotwords từ artifact nightly: %s", candidate)
                    return str(candidate)
            except OSError as exc:
                log.warning("Đọc con trỏ artifact lỗi: %s", exc)
        fallback = MODELS_DIR / "hotwords.txt"
        return str(fallback) if fallback.exists() else ""

    # -- device ------------------------------------------------------------
    def _pick_provider(self, lane: str) -> str:
        profile = load_device_profile().get(lane)
        requested = profile or (
            self.settings.device_interactive
            if lane == "interactive"
            else self.settings.device_batch
        )
        requested = (requested or "cpu").lower()
        if requested.startswith("cuda") or requested == "gpu":
            ok, reason = allow_gpu(self.settings)
            if ok:
                log.info("Lane %s dùng GPU: %s", lane, reason)
                return "cuda"
            log.info("Lane %s rơi về CPU: %s", lane, reason)
        return "cpu"

    # -- lifecycle ---------------------------------------------------------
    def recognizer(self, lane: str = "interactive"):
        with self._lock:
            if lane in self._recognizers:
                return self._recognizers[lane]
            provider = self._pick_provider(lane)
            try:
                rec = create_recognizer(
                    self.settings.model_dir,
                    self.settings,
                    provider=provider,
                    hotwords_file=self.hotwords_path if self.hotwords_path else "",
                    hotwords_score=self.settings.hotwords_score,
                )
            except Exception as exc:  # noqa: BLE001 - GPU build lỗi -> fallback CPU
                if provider != "cpu":
                    log.warning("Nạp GPU thất bại (%s), fallback CPU", exc)
                    provider = "cpu"
                    rec = create_recognizer(
                        self.settings.model_dir,
                        self.settings,
                        provider="cpu",
                        hotwords_file=self.hotwords_path,
                        hotwords_score=self.settings.hotwords_score,
                    )
                else:
                    raise
            self._recognizers[lane] = rec
            self._providers[lane] = provider
            return rec

    def provider_of(self, lane: str) -> str:
        return self._providers.get(lane, "not-loaded")

    def reload(self) -> dict[str, Any]:
        """Nạp lại hotwords (dùng SIGHUP hoặc /v1/admin/reload)."""
        with self._lock:
            self._recognizers.clear()
            self._providers.clear()
            self.hotwords_path = self._resolve_hotwords_path()
            model = self.settings.model_cfg["label"]
            try:
                self.recognizer("interactive")
                loaded = True
            except Exception as exc:  # noqa: BLE001 - báo cho admin, không crash
                log.error("Reload thất bại: %s", exc)
                loaded = False
            return {
                "reloaded": loaded,
                "model": model,
                "hotwords_path": self.hotwords_path,
                "provider": self.provider_of("interactive"),
            }

    # -- inference ---------------------------------------------------------
    def transcribe_pcm16(
        self, pcm: bytes, sample_rate: int = 16000, lane: str = "interactive"
    ) -> str:
        if not pcm:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return self.transcribe_samples(audio, sample_rate, lane)

    def transcribe_samples(
        self, audio: np.ndarray, sample_rate: int = 16000, lane: str = "interactive"
    ) -> str:
        if audio.size == 0:
            return ""
        if sample_rate != 16000:
            audio = resample(audio, sample_rate, 16000)
        rec = self.recognizer(lane)
        stream = rec.create_stream()
        stream.accept_waveform(16000, audio)
        rec.decode_stream(stream)
        return (stream.result.text or "").strip()


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample tuyến tính — đủ cho đầu vào ASR, giữ deps tối thiểu."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    duration = audio.size / float(src_rate)
    n_dst = int(round(duration * dst_rate))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    src_idx = np.linspace(0.0, audio.size - 1, num=n_dst, dtype=np.float64)
    return np.interp(src_idx, np.arange(audio.size), audio).astype(np.float32)


def settings_summary(settings: Settings) -> dict[str, Any]:
    """Snapshot cấu hình để log/health (không lộ secret — không có secret ở đây)."""
    data = asdict(settings)
    data["model_dir"] = str(settings.model_dir)
    data["model"] = settings.model_cfg["label"]
    data["model_ready"] = model_ready(settings)
    return data
