# Bảo mật — ranh giới trách nhiệm

## Quyết định thiết kế: service **không** có auth

`meilin-stt` cố tình không tự làm authentication/authorization. Lý do:

1. Auth phải khớp với **lớp ingress thật** của máy đích (Cloudflare Access, service token, mTLS, reverse proxy của agents). Làm auth nửa vời trong app sẽ tạo **cảm giác an toàn giả**.
2. Hai lớp auth (app + ingress) thường lệch nhau khi rotate credential → khó vận hành hơn.
3. Service chạy sau tunnel/proxy và chỉ bind loopback → bề mặt tấn công trực tiếp bằng 0.

**Auth là việc của agents/ingress phía trước.** Repo này chỉ đảm bảo:

- ✅ **Fail-closed**: bind ngoài loopback mà thiếu `STT_ALLOW_PUBLIC=1` → process **thoát ngay**.
- ✅ **Admin chỉ loopback**: `/v1/admin/*` trả `403` nếu request không đến từ `127.0.0.1`/`::1`.
- ✅ **Không log dữ liệu nhạy cảm**: log chỉ có model, provider, số term/rule — không log audio, không log nội dung text nhận dạng.
- ✅ **Không lưu audio**: `data/utterances.jsonl` chỉ có text + metadata; audio bị bỏ sau khi decode.
- ✅ **Không có secret trong repo**: `.env` bị gitignore; `.env.example` chỉ có biến không nhạy cảm.

## Threat model (ngắn)

| Mối đe doạ | Ảnh hưởng | Giảm thiểu |
|---|---|---|
| Người lạ gọi API khi đã bind public | Dùng ké CPU/GPU, đọc kết quả nhận dạng | Bắt buộc đặt sau ingress có auth; mặc định loopback |
| Path traversal qua `?file=` | Không có — API chỉ nhận **upload**, không nhận path | — |
| Upload file khổng lồ | Cạn RAM/disk | `ws_max_size`, trần `MemoryMax`, `TimeoutStartSec`; thêm giới hạn ở proxy |
| SSRF qua ffmpeg | ffmpeg nhận file, không nhận URL | Không expose tham số URL |
| Audio rò rỉ ra ngoài | Vi phạm riêng tư | Không có network outbound trong code (chỉ tải model lúc cài) |
| Cạn CPU/GPU làm chậm dịch vụ khác | Máy chia sẻ bị ảnh hưởng | `CPUQuota`, `MemoryMax`, nightly `Nice=19` + gate tài nguyên |
| Lộ token trong log | Chiếm quyền ingress | Không có token trong service; nếu ingress dùng token, không đưa vào `.env` của service |

## Checklist trước khi mở ra ngoài

- [ ] Service bind `127.0.0.1` (hoặc `0.0.0.0` nhưng **sau** tunnel/proxy có auth)
- [ ] `/v1/admin/*` không được route ra ngoài (xem `deploy/cloudflared.example.yml` — nhánh `http_status:404`)
- [ ] `.env` có `chmod 600`, owner là user chạy service
- [ ] Có rate limit ở ingress (WS mở nhiều connection = tốn CPU)
- [ ] Log retention: `journalctl` có `SystemMaxUse` hợp lý
- [ ] Đã thử: `curl` từ máy khác vào `/v1/admin/status` → phải `403`
- [ ] Đã thử: gửi feedback rác → không ảnh hưởng artifact đang chạy (gate chặn)

## Nếu Anh muốn thêm auth sau này

Chỗ cắm đã chuẩn bị:

1. **Ingress** (khuyến nghị): Cloudflare Access / nginx `auth_request` / mTLS. Không sửa code.
2. **Middleware FastAPI** trong `server/app.py`: thêm `@app.middleware("http")` kiểm bearer token từ env — nhưng khi đó phải rotate token đồng bộ với agents gọi.
3. **Khi thêm**: đặt token trong `/etc/meilin-stt/stt.env` (mode 600), **không** commit, **không** log giá trị, so sánh bằng `hmac.compare_digest`.

Nguyên tắc: nếu thêm auth vào service thì **bỏ** auth trùng ở ingress, tránh hai nguồn sự thật.
