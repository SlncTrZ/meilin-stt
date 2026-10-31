# Bộ regression đóng băng

Gate của nightly job so CER trên bộ này. Không có case → gate **không chặn** (và report ghi rõ là gate bị tắt), tức là mất lớp bảo vệ "train không phá".

## Thêm một case

1. Audio: **16 kHz, mono, PCM16 wav**. Convert:

   ```bash
   ffmpeg -i input.m4a -ar 16000 -ac 1 -c:a pcm_s16le 001.wav
   ```

2. Thêm vào `manifest.json`:

   ```json
   {
     "id": "001",
     "audio": "tests/fixtures/regression/001.wav",
     "expect": "mở Qdrant ra giúp anh",
     "max_cer": 0.15
   }
   ```

3. Khoá bất biến khi viết case:
   - `expect` là **ground truth do người viết**, không phải output của model.
   - `max_cer` đặt theo mục tiêu chất lượng (0.10–0.20 hợp lý); đừng đặt bằng CER hiện tại của model — làm vậy gate sẽ chấp nhận mọi thứ tệ hơn trong tương lai.
   - Có ít nhất: 5 câu có tên riêng/thuật ngữ trong lexicon, 5 câu thường, 2–3 câu nhiễu/ồn.

## Quy mô khuyến nghị

| Mục đích | Số case | Thời gian chạy (68M, CPU 4 thread) |
|---|---|---|
| Tối thiểu (để gate có tác dụng) | 5 | ~5–10 s |
| Tốt | 15–30 | ~30–60 s |
| Kỹ | 50+ | ~2 phút (vẫn trong `TimeoutStartSec=3600`) |

## Lưu ý

- Không commit audio riêng tư. Nếu case chứa giọng người thật → để trong `_private/` (gitignored) và trỏ đường dẫn tuyệt đối bằng biến môi trường ở máy chạy.
- Không dùng bộ này làm test-held-out cho fine-tune (L3). Cần bộ riêng, nếu không sẽ overfit vào gate.
