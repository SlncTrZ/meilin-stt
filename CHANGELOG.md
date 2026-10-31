# Changelog

Định dạng theo [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), version theo [SemVer](https://semver.org/lang/vi/).

## [Unreleased]

### Dự kiến
- L3 fine-tune/distill (chỉ sau khi có ≥5 giờ audio xác nhận + bộ eval held-out riêng)
- Silero VAD: đóng gói model + verify trên tiếng Việt (hiện là code chưa test)
- Dấu câu tiếng Việt (ViBERT-capu) áp lên `final` — cần cân nhắc license CC-BY-SA-4.0

## [0.1.0] — 2026-10-31

### Thêm
- API FastAPI: `GET /health`, `GET /v1/models`, `WS /ws` (PCM16 LE → `partial` + `final`),
  `POST /v1/transcribe` (batch, trả RTF + segments), `POST /v1/feedback`,
  `POST /v1/admin/reload` + `GET /v1/admin/status` (loopback-only).
- Engine: registry model `68m` / `30m`, hai lane `interactive` / `batch`, chọn thiết bị theo
  `data/device_profile.json` (kết quả `server.bench`), fallback CPU khi GPU bận.
- Vòng học lexicon: `mine` / `promote` / `demote` / `render` / `validate` / `audit` / `learn`.
- Hotwords phân tier A(2.5) / B(1.5) / C(1.1), seed **300 term**, validate bằng BPE thật của model.
- Correction layer (`replacements.json`) áp lên `final`, ưu tiên rule dài, khớp theo biên từ.
- Nightly job: gate tài nguyên (`ExecCondition`), regression gate CER, publish/rollback artifact,
  retention, report markdown/JSON.
- systemd unit + timer, Dockerfile + compose, ví dụ Cloudflare Tunnel.
- 50 test (unit + gate + API + engine) và integration test tách riêng (`-m integration`).

### Sửa
- `bpe_vocab` phải là vocab **TEXT** (`token score` mỗi dòng). Truyền `bpe.model` (binary
  sentencepiece) làm tầng C++ của sherpa-onnx thoát ngay với lỗi
  `Each line in vocab should contain two items …`, không có traceback Python.
  Nay `engine.ensure_bpe_vocab()` tự sinh `bpe.vocab` từ `bpe.model` (cache; fallback `data/cache/`
  khi model dir read-only).
- VAD: segment trước đây bị kéo dài thêm đúng `min_silence` (cắt tại frame phát hiện im lặng
  thay vì frame có tiếng nói cuối). Đã cắt đúng cuối tiếng nói.
- `apply_rules` trước đây thay tuần tự nên rule ngắn sửa chồng lên kết quả rule dài
  (`sherpa` ăn vào `sherpa-onnx` vừa tạo). Nay quét một lượt, ưu tiên match dài nhất.
- Mining nhiễu: thêm `looks_like_misrecognition()` + `is_ascii_term()` để loại "viết lại cả câu",
  chỉ giữ span thật sự là nghe sai/thuật ngữ.
- `/v1/feedback` giữ `raw_text`, nên CLI/hook không cần bảng `utterances` vẫn mine được.
