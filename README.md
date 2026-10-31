# meilin-stt

Offline Vietnamese STT service (sherpa-onnx) — **API để máy khác gọi**, kèm **vòng học lexicon**: hotwords phân tier, bảng sửa lỗi học từ feedback, regression gate, và job cập nhật ban đêm.

Dùng cho: một máy Linux có GPU/CPU chạy STT, các agent/Pi ở máy khác gọi qua HTTP/WebSocket.
Không gửi audio ra Internet. Không có auth trong service (xem [docs/security.md](docs/security.md) — auth do lớp ingress/agents phía trước lo).

```
  Pi / agent  ──WS PCM16──►  meilin-stt  ──►  sherpa-onnx (zipformer-vi-68M)
      │                          │
      └──POST /v1/feedback──►  data/feedback.jsonl
                                   │
                        nightly 03:00 (CPU, có gate tài nguyên)
                                   ▼
                    lexicon → hotwords.txt + replacements.json
                                   │
                        regression gate → publish/rollback
```

## Nhanh

```bash
git clone https://github.com/SlncTrZ/meilin-stt && cd meilin-stt
./scripts/install.sh                 # venv + model 68M + seed 300 hotwords + validate + smoke
cp .env.example .env

python -m server.app                 # hoặc: meilin-stt
curl -s localhost:8766/health | python -m json.tool
python clients/cli.py transcribe ./sample.wav
python clients/cli.py mic            # thu micro -> WS -> final
```

Docker:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

## API (hợp đồng đầy đủ: [server/protocol.md](server/protocol.md))

| Endpoint | Việc |
|---|---|
| `GET /health` | model, hotwords, provider, GPU headroom |
| `GET /v1/models` | registry model (68m / 30m) |
| `WS /ws` | stream PCM16 LE 16 kHz → `partial` + `final` |
| `POST /v1/transcribe` | transcribe file (mp3/wav/m4a…), trả RTF + segments |
| `POST /v1/feedback` | người dùng sửa text → dữ liệu học |
| `POST /v1/admin/reload` | (loopback) nạp lại hotwords/replacements |
| `GET /v1/admin/status` | (loopback) chi tiết vận hành |

Ví dụ WS:

```
client → server : binary PCM16LE frames
client → server : {"type":"end"}
server → client : {"partial":"xin chào các"}
server → client : {"final":"Xin chào các bạn.","utterance_id":"a1b2c3","corrections":[],"raw":"..."}
```

## Vòng học — "vừa dùng vừa train"

Hotwords của sherpa-onnx là **inference-time biasing**, không phải training. Nên "học" ở đây là 3 tầng không-gradient, đo được, rollback được:

| Tầng | Học gì | Chạy ở đâu | Chặn bởi |
|---|---|---|---|
| L1 lexicon | từ nào cần boost + score bao nhiêu | nightly (CPU) | regression gate |
| L2 correction table | cặp `sai → đúng` áp lên `final` | nightly (CPU) | regression gate |
| L3 fine-tune/distill | trọng số model | **thủ công**, cần GPU | không có trong repo này (xem docs/nightly.md) |

```bash
python -m lexicon.cli learn "mở Qdrant ra" --raw "mở quảng chạy ra" --model-dir ~/.cache/sherpa-onnx/zipformer-vi-68M
python -m lexicon.cli mine --min-support 2
python -m lexicon.cli promote --min-hits 2
python -m lexicon.cli render --model-dir "$MODEL_DIR" --publish
python -m lexicon.cli validate --model-dir "$MODEL_DIR"   # loại "hotword chết" (BPE không emit được)
python -m lexicon.cli audit
```

**Gate là bất biến:** artifact mới chỉ được publish khi CER trên bộ regression không tệ hơn baseline + tolerance. Gate fail → con trỏ `current` giữ nguyên (rollback tự động).

## Hotwords phân tier

| Tier | Score | Loại | Ví dụ |
|---|---|---|---|
| A | 2.5 | brand / tên riêng | `MEILIN`, `SLNCTRZ`, `QDRANT`, `N8N`, `SHERPA-ONNX` |
| B | 1.5 | thuật ngữ kỹ thuật | `ZIPFORMER-68M`, `HOTWORDS`, `SYSTEMD UNIT` |
| C | 1.1 | từ tiếng Việt phổ biến (score thấp để không bịa) | `BÁO CÁO`, `TRIỂN KHAI` |

Seed sẵn **300 term** (`models/hotwords.txt`, sinh bởi `scripts/seed_hotwords.py`). Tất cả đều được validate bằng BPE của model thật — hotword mà model không emit được sẽ bị cảnh báo và loại.

## Tính năng

- **2 đường suy luận**: `interactive` (mic, ưu tiên p95) và `batch` (file dài, ưu tiên RTF) — chọn thiết bị theo **số đo**, không theo cảm tính (`python -m server.bench --apply`).
- **Partial kết quả** trên WS (decode lại buffer, không cần streaming model).
- **Correction layer** áp lên `final` theo biên từ, ưu tiên rule dài, không sửa chồng.
- **VAD**: server-side energy VAD cho file dài; Silero VAD là tuỳ chọn (chưa verify).
- **Nightly job** có gate tài nguyên (`ExecCondition`) — máy chia sẻ với dịch vụ khác thì tự bỏ qua, chạy bù đêm sau.
- **Fail-closed**: bind ngoài loopback mà thiếu `STT_ALLOW_PUBLIC=1` → thoát.

## Tài liệu

| File | Nội dung |
|---|---|
| [server/protocol.md](server/protocol.md) | hợp đồng API (WS/HTTP), semantics, lỗi |
| [docs/deploy-linux.md](docs/deploy-linux.md) | systemd / Docker / Cloudflare Tunnel |
| [docs/security.md](docs/security.md) | ranh giới auth, fail-closed, checklist |
| [docs/nightly.md](docs/nightly.md) | vòng học, gate, rollback, L3 (chưa có) |
| [docs/benchmark.md](docs/benchmark.md) | cách đo RTF/latency, số đo mẫu, điểm chưa verify |
| [docs/gpu-concurrency.md](docs/gpu-concurrency.md) | dùng GPU trên máy đang bận (RTX 2080, Turing, không BF16) |
| [docs/migration-from-pi.md](docs/migration-from-pi.md) | chuyển từ worker cũ của Pi sang service này |

## Cấu trúc

```
server/    app.py (API) · engine.py (model/device) · vad.py · bench.py · protocol.md
lexicon/   store.py · mine.py · correct.py · render.py · pipeline.py · cli.py
nightly/   runner.py · gpu_gate.py · gates.py · report.py
clients/   cli.py · pi-voice-input/ (extension Pi gốc, để tham chiếu)
deploy/    *.service · *.timer · Dockerfile · docker-compose.yml · cloudflared.example.yml
scripts/   install.sh · install-gpu.sh · download-models.py · seed_hotwords.py · gpu-report.sh
tests/     37 test (lexicon, gates, vad, api, runner) + fixtures/regression/manifest.json
models/    hotwords.txt (sinh tự động)
data/      runtime: feedback/lexicon/artifacts/reports (không commit)
```

## Test

```bash
python -m pytest -q          # 37 passed
```

Test khoá **bất biến**, không khoá output: gate phải chặn khi tệ hơn baseline, admin phải chặn ngoài loopback, render phải loại entry `demoted`, correction không được sửa chồng rule.

## Ghi công / license

- Code: **MIT** (xem [LICENSE](LICENSE)).
- Engine: [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache-2.0).
- Model: `csukuangfj/sherpa-onnx-zipformer-vi-2025-04-20` và `hynt/Zipformer-30M-RNNT-6000h` — xem card trên Hugging Face; **repo này không phân phối weight**, chỉ tải lúc cài.
- Ý tưởng vòng học lexicon chịu ảnh hưởng từ [welcomyou/sherpa-vietnamese-asr](https://github.com/welcomyou/sherpa-vietnamese-asr) (hotwords + punctuation trong app của họ).
