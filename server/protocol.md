# meilin-stt — giao thức API

Tài liệu này là **hợp đồng** giữa client (Pi extension / CLI / bất kỳ ai) và server.
Thay đổi phá vỡ hợp đồng phải bump `API_VERSION` trong `server/app.py`.

- `API_VERSION`: `1`
- Audio format nội bộ: **PCM16 LE, mono, 16 kHz** (480 byte = 10 ms)
- Tất cả endpoint `/v1/*` trả JSON UTF-8, không bọc envelope.

## 1. `GET /health`

```json
{
  "status": "ok",
  "model": "zipformer-vi-68M",
  "model_ready": true,
  "model_dir": "/home/user/.cache/sherpa-onnx/zipformer-vi-68M",
  "hotwords_path": "/opt/meilin-stt/data/artifacts/20261031-0300/hotwords.txt",
  "hotwords_terms": 312,
  "replacement_rules": 14,
  "providers": { "interactive": "cpu", "batch": "cpu" },
  "gpu": { "name": "NVIDIA GeForce RTX 2080", "free_mb": 5120, "total_mb": 8192, "util_pct": 12 },
  "partial_enabled": true,
  "uptime_sec": 842.3
}
```

`gpu` = `"none"` nếu máy không có `nvidia-smi`. `providers.*` = `"not-loaded"` khi lane chưa được dùng.

## 2. `GET /v1/models`

```json
{
  "active": "68m",
  "available": {
    "68m": { "label": "zipformer-vi-68M", "hf_repo": "csukuangfj/sherpa-onnx-zipformer-vi-2025-04-20", "approx_ram_mb": 900 },
    "30m": { "label": "zipformer-vi-30M", "hf_repo": "hynt/Zipformer-30M-RNNT-6000h", "approx_ram_mb": 220 }
  }
}
```

## 3. `WS /ws` — đường interactive

Query: `?lane=interactive|batch&sample_rate=16000`

### Server → client

| Message | Khi nào |
|---|---|
| `{"type":"ready","lane":"interactive","sample_rate":16000}` | ngay sau accept |
| `{"partial":"xin chào các"}` | mỗi `STT_PARTIAL_INTERVAL_MS` (mặc định 500ms), chỉ khi đã có ≥ 0.8s audio |
| `{"final":"Xin chào các bạn.","utterance_id":"a1b2c3","corrections":[{"wrong":"..","right":"..","mode":"exact"}],"raw":"xin chào các bạn"}` | sau khi client gửi `{"type":"end"}` hoặc buffer chạm `STT_MAX_UTTERANCE_SEC` |
| `{"type":"reset_ok"}` / `{"type":"pong"}` | phản hồi control message |
| `{"error":"..."}` | decode lỗi (connection vẫn mở) |

`corrections` = các rule post-ASR đã áp lên `final` (rỗng nếu không áp gì). `raw` = text trước khi sửa — cần cho vòng feedback.

### Client → server

| Message | Ý nghĩa |
|---|---|
| **binary** frame PCM16LE | append vào buffer utterance hiện tại |
| `{"type":"end"}` | chốt utterance → nhận `final` |
| `{"type":"reset"}` | xoá buffer, không decode |
| `{"type":"ping"}` | health-check nhẹ |

**Semantics:** một connection có thể chứa **nhiều utterance** liên tiếp (mỗi lần `end` là một utterance). Client **tự làm VAD** — server không tự cắt im lặng trên đường interactive (đây là lý do độ trễ thấp).

**Partial cost:** partial được sinh bằng cách decode lại toàn bộ buffer hiện tại (không có streaming model). Với utterance < 20s chi phí chấp nhận được; tắt bằng `STT_PARTIAL=0` nếu máy yếu.

## 4. `POST /v1/transcribe` — đường batch

`multipart/form-data`: `file=@audio.mp3`

Query: `?apply_corrections=true&segment=true`

```json
{
  "text": "toàn bộ nội dung đã nhận dạng",
  "duration_sec": 312.4,
  "elapsed_sec": 21.7,
  "rtf": 0.069,
  "provider": "cpu",
  "segments": [{ "start": 0.0, "end": 4.8, "text": "..." }]
}
```

Định dạng nhận trực tiếp (không cần ffmpeg): `wav`, `flac`, `ogg` (soundfile).
Còn lại (`mp3`, `m4a`, `mp4`…) đi qua `ffmpeg` (`STT_FFMPEG`). Không có ffmpeg → `415`.

`segment=true` dùng VAD năng lượng server-side để chia đoạn ≤ 30s (tránh decode 1 phát audio quá dài).

## 5. `POST /v1/feedback`

```json
{ "utterance_id": "a1b2c3", "corrected_text": "Xin chào, mở Qdrant ra.", "raw_text": "xin chào mở quảng chạy ra", "source": "api" }
```

→ `{"ok": true, "utterance_id": "a1b2c3"}`

Dữ liệu này là **nguồn học duy nhất** của nightly job. Không gửi audio: chỉ text.

## 6. Admin (loopback-only)

| Endpoint | Việc |
|---|---|
| `POST /v1/admin/reload` | nạp lại hotwords (từ artifact `current`) + `replacements.json`; nạp lười lại ở request kế tiếp |
| `GET /v1/admin/status` | settings đầy đủ + provider + số feedback/utterances |

Nếu `STT_ADMIN_LOOPBACK_ONLY=1` (mặc định) và request không đến từ loopback → `403`.

## 7. Bảo mật — ranh giới rõ ràng

Service này **không có auth**. Auth/throttle/TLS do lớp trước đảm nhiệm
(agents của máy đích, Cloudflare Tunnel, reverse proxy, mTLS…). Xem `docs/security.md`.

Fail-closed: bind `0.0.0.0` mà thiếu `STT_ALLOW_PUBLIC=1` → process thoát ngay khi startup.
