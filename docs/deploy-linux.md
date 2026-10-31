# Deploy trên Linux

Ba cách, chọn một. Mặc định khuyến nghị: **systemd + venv** (đơn giản nhất để gate tài nguyên hoạt động đúng).

## 0. Chuẩn bị chung

```bash
sudo apt install -y python3.12 python3.12-venv ffmpeg libsndfile1
git clone https://github.com/SlncTrZ/meilin-stt /opt/meilin-stt
sudo chown -R "$USER":"$USER" /opt/meilin-stt
cd /opt/meilin-stt
```

Model nặng ~260MB (68M). Nếu máy RAM thấp / ưu tiên tốc độ: dùng `30m` (int8, ~33MB).

## 1. systemd + venv (khuyến nghị)

```bash
./scripts/install.sh --model 68m
cp .env.example .env && chmod 600 .env

sudo useradd -r -m -d /opt/meilin-stt meilin
sudo mkdir -p /etc/meilin-stt
sudo cp .env.example /etc/meilin-stt/stt.env
sudo chmod 600 /etc/meilin-stt/stt.env
sudo chown -R meilin:meilin /opt/meilin-stt/data /opt/meilin-stt/models

sudo cp deploy/meilin-stt.service deploy/meilin-stt-nightly.service \
        deploy/meilin-stt-nightly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meilin-stt.service
sudo systemctl enable --now meilin-stt-nightly.timer
```

Kiểm tra:

```bash
systemctl status meilin-stt --no-pager
curl -s http://127.0.0.1:8766/health | python3 -m json.tool
systemctl list-timers meilin-stt-nightly.timer
```

Reload hotwords thủ công (sau khi publish artifact): `sudo systemctl reload meilin-stt`.

### Trần tài nguyên đã đặt trong unit

| Tham số | Giá trị | Lý do |
|---|---|---|
| `MemoryMax` | 3G | 68M cần ~1GB; trần để không OOM cả máy |
| `CPUQuota` | 300% | 4 thread, chừa core cho dịch vụ khác |
| `Nice` / `IOSchedulingClass` | 5 / best-effort | service thường |
| nightly `Nice` / `IOSchedulingClass` | 19 / idle | job nền, nhường tuyệt đối |

## 2. Docker

```bash
cp .env.example .env && chmod 600 .env
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f meilin-stt
```

- Model nằm trong volume `meilin-stt-models` (không nằm trong image).
- `data/` mount từ host → feedback/lexicon/artifacts bền vững.
- Nightly trong Docker: dùng profile `nightly` hoặc (khuyến nghị) để **systemd timer trên host** gọi `docker compose run` — vì gate tài nguyên dùng `ExecCondition`, dễ kiểm soát hơn.
- Port chỉ mở `127.0.0.1:8766` → ingress đứng trước.

## 3. Cloudflare Tunnel (đưa ra ngoài, không mở port)

```bash
cloudflared tunnel create meilin-stt
cp deploy/cloudflared.example.yml ~/.cloudflared/meilin-stt.yml   # sửa tunnel id
chmod 600 ~/.cloudflared/*.json
cloudflared --config ~/.cloudflared/meilin-stt.yml tunnel run meilin-stt
```

Đặt làm systemd service: `sudo cloudflared service install <token>` (đọc log qua `journalctl -u cloudflared`).

⚠️ Tunnel chỉ đảm bảo TLS + đường đi. **Xác thực người gọi là việc của lớp agents/ingress** — xem [security.md](security.md).

## Bật GPU (tuỳ chọn, cần verify)

```bash
./scripts/install-gpu.sh          # in hướng dẫn build sherpa-onnx CUDA, không tự chạy
python -m server.bench --apply    # đo rồi mới chốt profile
```

RTX 2080 = Turing: FP16 được, **BF16 không**. Đọc [gpu-concurrency.md](gpu-concurrency.md) trước khi bật.

## Vận hành

| Việc | Lệnh |
|---|---|
| Xem log | `journalctl -u meilin-stt -f` |
| Reload hotwords | `sudo systemctl reload meilin-stt` |
| Chạy nightly tay | `sudo systemctl start meilin-stt-nightly` |
| Xem report | `cat data/reports/nightly-$(date +%F).md` |
| Rollback artifact | sửa `data/artifacts/current.txt` về stamp cũ rồi `systemctl reload` |
| Đo thiết bị | `python -m server.bench --apply` |
| Headroom GPU | `./scripts/gpu-report.sh` |

## Sự cố thường gặp

| Triệu chứng | Nguyên nhân | Xử lý |
|---|---|---|
| Process thoát ngay khi start | `STT_HOST` ngoài loopback mà thiếu `STT_ALLOW_PUBLIC=1` | Đây là fail-closed có chủ đích |
| `Thiếu file model ...` | chưa tải model | `python scripts/download-models.py --model 68m` |
| C-level exit: `Each line in vocab should contain two items` | truyền `bpe.model` (binary) vào `bpe_vocab` | Đã xử lý trong code: tự sinh `bpe.vocab` text từ `bpe.model` |
| `/v1/transcribe` trả 415 | không có ffmpeg | `apt install ffmpeg` hoặc set `STT_FFMPEG` |
| Nightly không chạy | gate tài nguyên fail | Xem `journalctl -u meilin-stt-nightly -n 30` — dòng `SKIP: ...` nói lý do |
