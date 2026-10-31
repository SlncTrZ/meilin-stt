"""Seed hotwords phân tier — chạy 1 lần để có lexicon khởi điểm (~350 term).

Wing: code | Topic: stt-seed-hotwords | Updated: 2026-10-31 23:07

Chạy:
  python scripts/seed_hotwords.py            # ghi data/lexicon.jsonl + models/hotwords.txt
  python scripts/seed_hotwords.py --dry-run  # chỉ in số lượng

Tier & score:
  A = 2.5  brand / tên riêng / sản phẩm — gần như không thể nhầm
  B = 1.5  thuật ngữ kỹ thuật, từ chuyên ngành
  C = 1.1  từ tiếng Việt phổ biến trong ngữ cảnh công việc (score thấp để tránh bịa)

Sau khi seed, luôn chạy:
  python -m lexicon.cli validate --model-dir <model_dir>
để loại các term mà BPE của model không emit được ("hotword chết").
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Console Windows mặc định cp1252 -> ép UTF-8 để log tiếng Việt không crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from lexicon import render  # noqa: E402
from lexicon.store import (  # noqa: E402
    LEXICON,
    STATUS_ACTIVE,
    TIER_SCORE,
    LexiconEntry,
    now_ts,
    save_lexicon,
)

TIER_A = """
MEILIN, SLNCTRZ, SCRESTTRD, PI, ZED, QDRANT, N8N, CLOUDFLARE, CLOUDFLARED, TAILSCALE,
GITHUB, GITLAB, DOCKER, DOCKER COMPOSE, KUBERNETES, POSTGRES, POSTGRESQL, REDIS, SQLITE,
OLLAMA, DEEPSEEK, GEMINI, CLAUDE, CLAUDE CODE, CHATGPT, COPILOT, CURSOR, WINDSURF, AIDER,
ONNX, ONNX RUNTIME, SHERPA ONNX, ZIPFORMER, WHISPER, SILERO, SILERO VAD, FASTAPI, UVICORN,
WEBSOCKET, SYSTEMD, SYSTEMD TIMER, NGINX, TRAEFIK, COOLIFY, SUPABASE, PINECONE, WEAVIATE,
MILVUS, MCP, LSP, SSE, JSONL, YAML, TOML, TYPESCRIPT, JAVASCRIPT, PYTHON, RUST, GOLANG,
NODEJS, REACT, NEXTJS, VITE, TAILWIND, SHADCN, GRADIO, STREAMLIT, JUPYTER, PANDAS, NUMPY,
PYTORCH, TENSORFLOW, CUDA, CUDNN, NVIDIA, RTX 2080, AMD, INTEL, UBUNTU, DEBIAN, WSL,
POWERSHELL, HYPER-V, PROXMOX, TRUENAS, SYNOLOGY, HUGGING FACE, MISTRAL, LLAMA, QWEN, GEMMA,
GROQ, OPENROUTER, CEREBRAS, VIBERT, PYANNOTE, CAM++, DNSMOS, CONVTASNET, ROVER, SENTENCEPIECE,
TRƯƠNG CÔNG ĐỊNH, SLNC TRZ, MEI LIN, GIAO THỨC MCP, CYBER BRAIN, CYBERBRAIN
"""

TIER_B = """
ZIPFORMER-68M, ZIPFORMER-30M, VOSK, PARAKEET, SENSEVOICE, HOTWORD, HOTWORDS, CONTEXT GRAPH,
BPE, TOKENIZER, EMBEDDING, VECTOR DB, RAG, LLM, ASR, STT, TTS, VAD, CER, WER, RTF, P95, P50,
LATENCY, THROUGHPUT, BENCHMARK, REGRESSION, PIPELINE, CRONJOB, HEALTH CHECK, ROLLBACK,
SNAPSHOT, ARTIFACT, MIGRATION, ADR, ROADMAP, BACKLOG, CHECKPOINT, CONTEXT TOKEN, GATEWAY,
OAUTH, PKCE, JWT, MTLS, TLS, BEARER TOKEN, API KEY, WEBHOOK, RATE LIMIT, IDEMPOTENT, ATOMIC,
FAIL CLOSED, LEAST PRIVILEGE, THREAT MODEL, INVARIANT, DETERMINISTIC, IMMUTABLE, SANDBOX,
STREAMING, PARTIAL RESULT, FINAL RESULT, SAMPLE RATE, PCM16, MONO, FFMPEG, VAD SEGMENT,
AUDIO CAPTURE, DIARIZATION, SPEAKER EMBEDDING, PUNCTUATION, PSEUDO LABEL, DISTILLATION,
FINE TUNE, OVERFITTING, BATCH SIZE, GRADIENT CHECKPOINT, FP16, INT8, QUANTIZATION, INFERENCE,
MODEL REGISTRY, WARM LOAD, COLD START, MEMORY FOOTPRINT, VRAM, HEADROOM, COTENANCY,
SYSTEMD UNIT, EXEC CONDITION, ONFAIL, RESTART POLICY, JOURNALCTL, SYSLOG, TELEMETRY,
OBSERVABILITY, METRICS, TRACING, SLO, SLA, PAGER DUTY, RUNBOOK, POSTMORTEM, INCIDENT,
FEATURE FLAG, CANARY, BLUE GREEN, ROLLOUT, ROLLBACK PLAN, BACKUP, RESTORE, DISASTER RECOVERY,
TRÍ TUỆ NHÂN TẠO, HỌC MÁY, HỌC SÂU, MẠNG NƠ-RON, XỬ LÝ NGÔN NGỮ TỰ NHIÊN,
NHẬN DẠNG GIỌNG NÓI, TỔNG HỢP GIỌNG NÓI, DỮ LIỆU HUẤN LUYỆN, MÔ HÌNH NGÔN NGỮ,
CHUYỂN ĐỔI SỐ, TỰ ĐỘNG HOÁ, HẠ TẦNG, ĐIỆN TOÁN ĐÁM MÂY, MÁY CHỦ ẢO, SAO LƯU, PHỤC HỒI,
GIÁM SÁT, NHẬT KÝ, CẢNH BÁO, QUY TRÌNH, KIẾN TRÚC HỆ THỐNG, MÃ NGUỒN MỞ, GIT COMMIT,
PULL REQUEST, CI CD, UNIT TEST, INTEGRATION TEST, END TO END, KIỂM THỬ TỰ ĐỘNG,
CƠ SỞ DỮ LIỆU, TRUY VẤN, CHỈ MỤC, BĂM MẬT KHẨU, XÁC THỰC, PHÂN QUYỀN, MÃ HOÁ,
CHỨNG CHỈ SSL, TÊN MIỀN, BĂNG THÔNG, ĐỘ TRỄ, TẢI ĐỒNG THỜI, GIỚI HẠN TỐC ĐỘ
"""

TIER_C = """
BÁO CÁO, TÀI LIỆU, KẾ HOẠCH, TIẾN ĐỘ, NGHIỆM THU, KIỂM THỬ, TRIỂN KHAI, BẢO TRÌ, TỐI ƯU,
SỰ CỐ, PHƯƠNG ÁN, GIẢI PHÁP, YÊU CẦU, CHỨC NĂNG, GIAO DIỆN, HIỆU NĂNG, CHI PHÍ, RỦI RO,
NGÂN SÁCH, NHÂN SỰ, QUY ĐỊNH, TIÊU CHUẨN, ĐÁNH GIÁ, ĐỀ XUẤT, PHÊ DUYỆT, BÀN GIAO,
HỢP ĐỒNG, KHÁCH HÀNG, NHÀ CUNG CẤP, LỊCH TRIỂN KHAI, BIÊN BẢN, TỔNG HỢP, PHÂN TÍCH,
THỐNG KÊ, BÁO CÁO TUẦN, BÁO CÁO THÁNG, MỤC TIÊU, KẾT QUẢ, HẠN CHẾ, GIẢI PHÁP KỸ THUẬT
"""


def _terms(block: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in block.replace("\n", ",").split(","):
        term = " ".join(raw.strip().split())
        if not term or term.upper() in seen:
            continue
        seen.add(term.upper())
        out.append(term)
    return out


def build_entries() -> list[LexiconEntry]:
    ts = now_ts()
    entries: list[LexiconEntry] = []
    for tier, block in (("A", TIER_A), ("B", TIER_B), ("C", TIER_C)):
        for term in _terms(block):
            entries.append(
                LexiconEntry(
                    term=term,
                    tier=tier,
                    score=TIER_SCORE[tier],
                    status=STATUS_ACTIVE,
                    hits=0,
                    created=ts,
                    updated=ts,
                    note="seed",
                )
            )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merge", action="store_true", help="giữ entry cũ, chỉ thêm term mới")
    parser.add_argument("--out", type=Path, default=BASE_DIR / "models" / "hotwords.txt")
    args = parser.parse_args(argv)

    seeded = build_entries()
    counts: dict[str, int] = {}
    for entry in seeded:
        counts[entry.tier] = counts.get(entry.tier, 0) + 1
    print(f"Seed: {len(seeded)} term — tier A={counts.get('A', 0)} B={counts.get('B', 0)} C={counts.get('C', 0)}")
    if args.dry_run:
        return 0

    if args.merge:
        from lexicon.store import load_lexicon

        existing = load_lexicon()
        known = {e.term.upper() for e in existing}
        entries = existing + [e for e in seeded if e.term.upper() not in known]
    else:
        entries = seeded
    save_lexicon(entries, LEXICON)
    print(f"Đã ghi {LEXICON}")

    path, count = render.render_hotwords(
        entries,
        out_path=args.out,
        include_candidates=False,
        header=(
            "Seed bởi scripts/seed_hotwords.py — KHÔNG sửa tay.\n"
            "Sau khi sửa lexicon: python -m lexicon.cli render --model-dir <model_dir> --publish"
        ),
    )
    print(f"Đã ghi {path} ({count} term)")
    print("Tiếp theo: python -m lexicon.cli validate --model-dir <model_dir>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
