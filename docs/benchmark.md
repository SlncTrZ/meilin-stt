# Benchmark — đo, đừng đoán

## Cách đo

```bash
python -m server.bench                              # đo CPU (và GPU nếu có headroom)
python -m server.bench --sample ./cau5s.wav         # dùng audio thật (khuyến nghị)
python -m server.bench --apply                      # chốt data/device_profile.json
```

`bench` đo 2 thứ **riêng biệt**:

| Số | Ý nghĩa | Dùng cho |
|---|---|---|
| `mean_ms` | độ trễ decode trung bình | throughput |
| `p95_ms` | độ trễ phân vị 95 | **cảm nhận người dùng** (voice input) |
| `load_sec` | thời gian nạp model | cold start |
| `rtf` | thời gian / độ dài audio | so sánh giữa máy |

`--apply` ghi `data/device_profile.json`; `engine._pick_provider()` đọc file này **trước** env → lane `interactive` và `batch` có thể chọn thiết bị khác nhau.

## Số đo trên máy dev (Windows 11, CPU-only, KHÔNG phải máy đích)

Đo bằng `python -m server.bench` với audio sinh tổng hợp (3–4s), model 68M, 4 thread:

| Chỉ số | Giá trị | Ghi chú |
|---|---|---|
| model load | ~1.5–3 s | 68M, 4 thread |
| decode 3s audio | ~1.2 s | ≈ 0.4 RTF khi máy đang bận việc khác |
| decode im lặng → text | `"Ừ"` | ⚠️ model bịa trên im lặng — **lý do client phải VAD** |

⚠️ Đây **không phải** con số để trích dẫn. Phải chạy lại trên máy đích.

## Số kỳ vọng trên RTX 2080 (ước lượng — PHẢI verify)

| Đường | CPU 4 thread | GPU fp16 | Ghi chú |
|---|---|---|---|
| Mic 3–5s | ~200–350 ms | ~60–120 ms | Tổng độ trễ người dùng thấy còn VAD trailing 400–500ms |
| File 1 giờ (`/v1/transcribe`) | ~3–5 phút | ~30–70 s | GPU thắng rõ nhất ở đây |
| Dấu câu ViBERT (final) | ~80–200 ms | ~15–40 ms | Nếu bật (hiện chưa tích hợp) |

## Điểm CHƯA verify (trung thực)

| Hạng mục | Trạng thái |
|---|---|
| `sherpa-onnx` wheel có CUDA | ❌ PyPI là CPU-only; phải build `SHERPA_ONNX_ENABLE_GPU=ON` — chưa chạy thử trên RTX 2080 |
| RTX 2080 + CUDA/cuDNN pairing | ❌ chưa xác nhận phiên bản (Turing: fp16 OK, bf16 KHÔNG) |
| Silero VAD (`server/vad.py::SileroVad`) | ❌ code có, chưa có model + chưa test tiếng Việt; mặc định không dùng |
| Tác dụng thật của hotwords | ⚠️ phụ thuộc dữ liệu — cần bộ regression để chứng minh (CER giảm), không tin cảm giác |
| Partial trên audio dài | ⚠️ chi phí tăng bậc 2 theo độ dài (decode lại buffer); utterance > 20s nên tắt partial |

## Vì sao hotwords có thể *làm tệ đi*

Context graph cộng điểm cho token của từ khoá. Với audio nhiễu, model có thể **bịa** từ khoá (score cao = ưu tiên mạnh). Do đó:

1. Score phân tier (A 2.5 / B 1.5 / C 1.1), không dùng 1 score cho tất cả.
2. Gate CER bắt buộc trước khi publish.
3. `lexicon demote <term> --reason "..."` khi một term gây bịa.

## RTF tham chiếu

`rtf = elapsed_sec / duration_sec`. `< 1` = nhanh hơn thời gian thực. `/v1/transcribe` trả `rtf` trong response để Anh theo dõi trực tiếp trên máy đích.
