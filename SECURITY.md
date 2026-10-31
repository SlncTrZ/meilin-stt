# Security Policy

**`meilin-stt` không có auth trong service.** Đây là quyết định thiết kế có chủ đích: xác thực người gọi thuộc lớp ingress/agents phía trước (Cloudflare Access, service token, mTLS, reverse proxy).

Những gì service tự đảm bảo:

- **Fail-closed**: bind ngoài loopback mà thiếu `STT_ALLOW_PUBLIC=1` → process thoát ngay khi startup.
- **Admin chỉ loopback**: `/v1/admin/*` trả `403` nếu request không đến từ `127.0.0.1`/`::1` (`STT_ADMIN_LOOPBACK_ONLY=1`).
- **Không lưu audio**: chỉ giữ text + metadata trong `data/utterances.jsonl`.
- **Không log nội dung nhận dạng / audio / secret**: log chỉ có model, provider, số term/rule.
- **Giới hạn upload**: `STT_MAX_UPLOAD_MB` (mặc định 200MB) → `413` khi vượt.
- **Không outbound network** trong code (ngoại lệ duy nhất: tải model lúc cài).

Chi tiết đầy đủ + threat model + checklist trước khi mở ra ngoài: **[docs/security.md](docs/security.md)**.

## Báo lỗi bảo mật

Mở private security advisory tại `https://github.com/SlncTrZ/meilin-stt/security/advisories/new` — **không** mở public issue cho lỗ hổng chưa vá.

Kèm theo: phiên bản commit, cách tái hiện, ảnh hưởng dự kiến (rò rỉ dữ liệu / DoS / chiếm quyền).
