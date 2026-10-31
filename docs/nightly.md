# Vòng học ban đêm (nightly)

## Triết lý

Hotwords của sherpa-onnx là **inference-time biasing** — không có weight nào được cập nhật. Vì vậy "train" ở đây nghĩa là: **học có kiểm soát, đo được, rollback được**, không phải gradient descent.

Ba tầng, tách biệt hoàn toàn:

| Tầng | Nội dung | Tài nguyên | Tự động? |
|---|---|---|---|
| **L1** lexicon | term + tier + score → `hotwords.txt` | CPU ~2 phút | ✅ mỗi đêm |
| **L2** correction table | cặp `sai → đúng` → `replacements.json` | CPU ~0 giây | ✅ mỗi đêm |
| **L3** fine-tune/distill | trọng số zipformer | GPU, giờ | ❌ **chưa có** (xem cuối file) |

## Luồng một đêm

```
03:00  meilin-stt-nightly.timer (RandomizedDelaySec=15min, Persistent=true)
   │
   ├─ ExecCondition: nightly.gpu_gate     ← máy bận thì SKIP (không phải failure)
   ├─ mine       : feedback.jsonl → ứng viên → lexicon.jsonl
   ├─ promote    : candidate → active khi hits ≥ ngưỡng
   ├─ render     : data/artifacts/<stamp>/{hotwords.txt, replacements.json, metrics.json}
   ├─ gate       : CER trên tests/fixtures/regression/manifest.json
   │      pass → publish (current.txt) → systemctl reload meilin-stt
   │      fail → giữ con trỏ cũ, xoá snapshot, report ❌
   └─ retention  : xoá artifact cũ (giữ 14 bản + bản đang current)
```

Chạy tay:

```bash
python -m nightly.runner --dry-run          # không ghi lexicon, không publish
python -m nightly.runner --force            # bỏ qua gate tài nguyên (dùng khi máy rảnh)
python -m nightly.runner --skip-regression  # chỉ khi chưa có bộ case
python -m nightly.runner --json
```

## Gate tài nguyên — vì sao phải có

Máy đích chạy nhiều dịch vụ khác. `nightly/gpu_gate.py` là `ExecCondition`, exit 1 → systemd **bỏ qua** unit (không tính là failed), `Persistent=true` sẽ chạy bù đêm sau.

Ngưỡng mặc định trong `deploy/meilin-stt-nightly.service`:

```
--min-free-mb 1500   # VRAM trống tối thiểu
--max-util 20        # GPU util tối đa
--min-free-cpu-pct 25
--min-free-mem-gb 0.5
```

Nếu máy liên tục không đủ headroom, job sẽ không bao giờ chạy → kiểm tra bằng:

```bash
journalctl -u meilin-stt-nightly -n 20
# thấy "SKIP: GPU util cao nhất 88% > 20%" = gate đang làm việc đúng
```

## Regression gate — bất biến "train không phá"

`nightly/gates.py`:

- Chuẩn hoá text (NFC, lowercase, bỏ dấu câu) → tính **CER** theo ký tự.
- Fail nếu: một case vượt `max_cer`, **hoặc** mean CER tệ hơn baseline (metrics của artifact đang chạy) + `--tolerance` (mặc định 0.01).
- Manifest rỗng → gate **không chặn** nhưng ghi lý do rõ ràng (để không tạo cảm giác an toàn giả: report sẽ ghi "regression gate bị tắt hoặc chưa có bộ case").

Thêm case (khuyến nghị 15–30 câu, có cả tên riêng và câu thường):

```bash
# 1. Thu 16kHz mono PCM16
ffmpeg -i input.m4a -ar 16000 -ac 1 -c:a pcm_s16le tests/fixtures/regression/001.wav
# 2. Thêm vào manifest
python - <<'PY'
import json, pathlib
p = pathlib.Path('tests/fixtures/regression/manifest.json')
data = json.loads(p.read_text(encoding='utf-8'))
data['cases'].append({
  "id": "001",
  "audio": "tests/fixtures/regression/001.wav",
  "expect": "mở Qdrant ra giúp anh",   # ground truth do người viết, KHÔNG copy output model
  "max_cer": 0.15,
})
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
PY
```

## Artifact & rollback

```
data/artifacts/
  20261031-0300/{hotwords.txt, replacements.json, metrics.json}
  20261101-0300/{...}
  current.txt            ← chứa stamp đang dùng
```

- `publish_artifacts()` = ghi `current.txt` + mirror `hotwords.txt`/`replacements.json` sang `models/` (fallback cho ai không dùng artifact).
- Rollback: sửa `current.txt` về stamp cũ rồi `systemctl reload meilin-stt` (hoặc `POST /v1/admin/reload` từ loopback).
- `retention(keep=14)` **không bao giờ xoá** artifact đang current.

## Ngưỡng học (tinh chỉnh)

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--min-support` | 2 | số lần một term phải xuất hiện trong feedback mới thành ứng viên |
| `--min-hits` | 2 | số hits tích luỹ để candidate thành active |
| `--min-support-rule` | 1 | số lần một cặp `sai→đúng` phải lặp lại mới thành rule |
| `--tolerance` | 0.01 | cho phép CER tệ hơn baseline 1% (tuyệt đối) |

Chống nhiễu đã cài sẵn:

- Span > 4 từ bị loại (người dùng viết lại cả câu, không phải thuật ngữ).
- `looks_like_misrecognition()`: chỉ nhận khi span **giống về ký tự** (≥0.55), **hoặc** là thuật ngữ kỹ thuật, **hoặc** term ASCII không dấu ≥4 ký tự (brand ngoại lai).
- Từ chức năng tiếng Việt (stopwords) bị loại.
- Hotword không tokenize được bằng BPE của model → loại ("hotword chết").

## L3 — fine-tune / distill (CHƯA có trong repo)

Nói thẳng: đây là phần **chưa triển khai**, và không nên triển khai mù.

Điều kiện tối thiểu trước khi nghĩ tới:

1. **Dữ liệu**: ≥ 5 giờ audio đã xác nhận (có transcript đúng) — không phải vài trăm câu.
2. **Phần cứng**: RTX 2080 8GB = Turing → FP16 được, **BF16 không**. Fine-tune zipformer-30M khả thi với micro-batch + gradient checkpoint; distill từ teacher 1.7B thì rất sát trần.
3. **Đánh giá**: phải có bộ test held-out riêng, không dùng lại manifest của gate.
4. **Tiêu chí thay thế model**: CER giảm ≥ 1% tuyệt đối so với model đang chạy, nếu không thì **không thay**.

Quy trình đúng khi làm: chạy **thủ công** (`meilin-stt train --max-minutes 90`), có checkpoint, có trần tài nguyên, không đưa vào cron cho tới khi chứng minh ổn định ≥ 4 lần liên tiếp. Lý do: job này cần nhiều VRAM nhất, chạy lâu nhất — trên máy chia sẻ, tự động hoá nó là cách chắc chắn nhất để đụng dịch vụ khác.

## Report

`data/reports/nightly-YYYY-MM-DD.md` (+ `.json` cùng ngày) gồm: feedback pairs, ứng viên mới, promoted, số hotword/rule, bảng CER từng case, lý do chặn (nếu fail), rollback target.
