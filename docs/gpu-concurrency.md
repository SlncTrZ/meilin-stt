# Dùng GPU khi máy đang bận (RTX 2080)

## Bối cảnh

Máy đích chạy nhiều dịch vụ khác. GPU **không phải của `meilin-stt`**. Nguyên tắc:

> STT là khách. Mặc định CPU-only, 0 byte VRAM. Mọi lần chạm GPU đều qua gate + có trần + có fallback.

## Phần cứng: RTX 2080 = Turing (compute 7.5)

| Đặc điểm | Hệ quả |
|---|---|
| Có FP16 | Dùng fp16, đừng fp32 nếu bật GPU |
| **Không có BF16** | Không bao giờ bật bf16/`--bf16`; PyTorch sẽ fallback chậm hoặc lỗi |
| 8GB VRAM | Teacher 1.7B (fp16 ≈3.4GB) + student chỉ vừa khi dùng gradient checkpoint |
| Không có NVLink/NVSwitch | Không multi-GPU |

## Hai đường suy luận, hai chính sách

| Lane | Dùng cho | Mặc định | Khi bật GPU |
|---|---|---|---|
| `interactive` | mic → text | **CPU** | Chỉ khi p95 đo được tốt hơn hẳn **và** GPU đang rảnh |
| `batch` | `/v1/transcribe` file dài | **CPU** | GPU thắng 5–8× — đáng bật khi headroom cho phép |

Chọn bằng số đo, không bằng cảm tính:

```bash
python -m server.bench --apply     # ghi data/device_profile.json
cat data/device_profile.json
```

## `allow_gpu()` — điều kiện duy nhất để chạm GPU

`server/engine.py`:

```
nvidia-smi --query-gpu=memory.free,utilization.gpu
→ cần free_mb ≥ STT_GPU_MIN_FREE_MB (1500)  VÀ  util ≤ STT_GPU_MAX_UTIL (40)
→ không đạt thì trả "cpu" + log lý do, KHÔNG raise
```

Nghĩa là: GPU bận → tự rơi về CPU giữa các request, không crash, không treo.

## Job nền: gate trước, không chen ngang

`deploy/meilin-stt-nightly.service`:

```ini
ExecCondition=... -m nightly.gpu_gate --min-free-mb 1500 --max-util 20 --samples 3
Nice=19
IOSchedulingClass=idle
CPUQuota=150%
MemoryMax=2G
```

Không đạt → systemd skip unit (không phải failure), `Persistent=true` chạy bù đêm sau.

## Nếu vẫn muốn dùng GPU cho job dài

Bắt buộc 4 điều:

1. **Trần VRAM**: `torch.cuda.set_per_process_memory_fraction(0.25)` — để OOM không lan sang process khác.
2. **Chống phân mảnh**: `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`.
3. **Giảm tranh chấp stream**: `CUDA_DEVICE_MAX_CONNECTIONS=1`.
4. **Nhả SM theo nhịp**: xử lý batch 8 → `sleep 0.2` → batch tiếp (không giữ SM liên tục).

```bash
# ví dụ chạy job dài có trần
CUDA_DEVICE_MAX_CONNECTIONS=1 \
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
nice -n 19 python -m nightly.runner --force
```

## Build sherpa-onnx có CUDA (chưa verify)

```bash
sudo apt install -y cmake build-essential git
git clone https://github.com/k2-fsa/sherpa-onnx /tmp/sherpa-onnx && cd /tmp/sherpa-onnx
export SHERPA_ONNX_CMAKE_ARGS="-DSHERPA_ONNX_ENABLE_GPU=ON -DCMAKE_CUDA_ARCHITECTURES=75"
pip install -e .          # trong venv của meilin-stt
```

`75` = Turing. Lấy đúng số bằng `nvidia-smi --query-gpu=compute_cap --format=csv`.

Sau khi build: `python -m server.bench --apply`. Nếu `providers.batch` vẫn là `cpu` → xem log `Nạp GPU thất bại (...), fallback CPU`.

## Giám sát

```bash
./scripts/gpu-report.sh 30 5      # 30 mẫu, 5s/lần → data/gpu-report.log
python -m nightly.gpu_gate --json # trạng thái gate hiện tại
curl -s localhost:8766/health | python -m json.tool | grep -A5 gpu
```

## Khi nào nên tắt GPU vĩnh viễn

- p95 latency trên GPU **tệ hơn** CPU (thường do GPU bị dịch vụ khác chiếm).
- `nvidia-smi` cho thấy util > 60% phần lớn thời gian → CPU ổn định hơn cho voice input.
- Cold start GPU (CUDA context 1–3s) làm request đầu tiên quá chậm so với nhịp dùng.

Trong các trường hợp đó: giữ `STT_DEVICE_INTERACTIVE=cpu`, `STT_DEVICE_BATCH=cpu` — đây cũng là mặc định của repo.
