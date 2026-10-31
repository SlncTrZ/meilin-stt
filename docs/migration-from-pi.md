# Chuyển từ worker cũ của Pi sang service này

Trước đây STT chạy **trong** Pi: extension `voice-input` tự spawn `sherpa_worker.py` (một file ~400 dòng, `ws://127.0.0.1:8766/ws`, chỉ trả `final`, không hotwords).

Service này thay phần **server**: worker chạy trên máy Linux riêng, Pi/agent chỉ là client.

## Khác biệt cần biết

| | Worker cũ (trong Pi) | meilin-stt |
|---|---|---|
| Vị trí | cùng máy Pi | máy khác, gọi qua mạng |
| Auth | không cần (loopback) | **do ingress/agents phía trước** |
| Hotwords | ❌ | ✅ 300+ term phân tier, học từ feedback |
| Correction layer | ❌ | ✅ bảng `sai→đúng` áp lên `final` |
| Partial | ❌ (chỉ final) | ✅ `{"partial": "..."}` |
| Batch transcribe | ❌ | ✅ `POST /v1/transcribe` (mp3/m4a…) |
| Model | 68M (default) / 30M | như trên + registry mở rộng được |
| Deps | sherpa-onnx + fastapi | + soundfile/ffmpeg (chỉ cho batch) |

## Cách 1 — Pi dùng làm STT server (không sửa code extension)

Extension `voice-input` đọc config từ `~/.pi/agent/voice-input.json`. Chỉ cần trỏ URL sang máy mới:

```json
{
  "workerProtocol": "websocket",
  "websocketUrl": "ws://<host-máy-stt>:8766/ws",
  "workerHost": "<host-máy-stt>",
  "workerPort": 8766,
  "workerPath": "/ws",
  "autoLaunchWorker": false
}
```

Bắt buộc `autoLaunchWorker: false` — nếu không extension sẽ cố spawn worker local và fail.

Giao thức tương thích: binary PCM16 → `{"type":"end"}` → `{"final": "..."}`. Service trả **thêm** `partial`, `utterance_id`, `corrections`, `raw` — extension cũ bỏ qua các field lạ nên không vỡ.

Qua Cloudflare Tunnel thì dùng `wss://<domain>/ws` và để auth ở ingress.

## Cách 2 — bật vòng feedback (khuyến nghị)

Worker cũ không gửi gì về sau khi trả text, nên không học được. Để vòng học chạy, cần client gửi correction:

- **Có `utterance_id`**: service trả kèm trong `final`; khi Anh sửa text, POST:
  ```bash
  curl -X POST http://<host>:8766/v1/feedback -H 'content-type: application/json' \
    -d '{"utterance_id":"a1b2c3","corrected_text":"mở Qdrant ra","raw_text":"mở quảng chạy ra"}'
  ```
- **Không có id** (chưa sửa extension): dùng CLI, vẫn học được:
  ```bash
  python -m lexicon.cli learn "mở Qdrant ra" --raw "mở quảng chạy ra" \
      --model-dir ~/.cache/sherpa-onnx/zipformer-vi-68M
  ```

Muốn tự động 100%: sửa extension để gửi `/v1/feedback` khi text bị chỉnh trước khi submit. Đây là việc **ở repo Pi**, không thuộc repo này (bản extension gốc được lưu ở `clients/pi-voice-input/` để tham chiếu, không phải bản để sửa tại đây).

## Cách 3 — agent/tool khác gọi trực tiếp

```bash
# batch
curl -F "file=@meeting.m4a" "http://<host>:8766/v1/transcribe" | jq .text

# realtime bằng Python
python clients/cli.py --http http://<host>:8766 --ws ws://<host>:8766/ws mic
```

## Kiểm tra sau khi chuyển

- [ ] `curl http://<host>:8766/health` → `model_ready: true`, `hotwords_terms` > 0
- [ ] Ghi âm thử trong Pi → text hiện ra, độ trễ chấp nhận được
- [ ] `/v1/admin/status` từ máy khác → **phải 403** (nếu 200 = đang hở, xem `docs/security.md`)
- [ ] Chạy `python -m nightly.runner --dry-run` trên máy STT → report sinh ra, không publish
- [ ] Đo lại: `python -m server.bench --apply`

## Không cần làm

- Không cần copy model sang máy Pi — model nằm trên máy STT.
- Không cần ffmpeg trên máy Pi (chỉ máy STT cần, và chỉ cho batch).
- Không cần mở port trên máy Pi.
