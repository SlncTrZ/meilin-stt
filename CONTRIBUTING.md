# Đóng góp

Repo cá nhân (SlncTrZ) nhưng mở — PR/báo lỗi đều được. Ưu tiên: **thay đổi nhỏ, có test, không phá bất biến**.

## Bất biến không được phá

1. **Gate phải chặn được artifact tệ hơn.** Không có regression case thì gate không có tác dụng — thêm case trước khi thêm hotword.
2. **`/v1/admin/*` chỉ loopback.** Không nới ra ngoài mà không có ingress auth.
3. **Fail-closed khi bind public.** `STT_HOST` ngoài loopback mà thiếu `STT_ALLOW_PUBLIC=1` → phải thoát.
4. **Correction chỉ áp lên `final`**, không đụng `partial`.
5. **Không lưu audio.** `data/utterances.jsonl` chỉ text + metadata.
6. **Không có auth trong service.** Auth thuộc ingress/agents phía trước (xem `docs/security.md`).

## Quy trình

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q                     # 50 unit test, không cần model
python scripts/download-models.py       # chỉ khi cần test suốt
STT_RUN_INTEGRATION=1 python -m pytest -m integration -q   # cần model thật
```

Trước khi mở PR:

- [ ] `ruff check .` sạch
- [ ] `pytest -q` xanh
- [ ] Có test cho hành vi mới (khoá **why**, không chỉ **what**)
- [ ] Nếu đổi giao thức: cập nhật `server/protocol.md` + bump `API_VERSION`
- [ ] Nếu đổi hành vi học: cập nhật `docs/nightly.md` + `CHANGELOG.md`
- [ ] Không commit `.env`, `*.onnx`, `data/*.jsonl`, `AGENTS.md`

## Kiểu code

- Docstring tiếng Việt có timestamp `YYYY-MM-DD HH:MM`; identifier/log message tiếng Anh hoặc Việt đều được, miễn nhất quán trong file.
- Comment giải thích **vì sao**, không kể lại code.
- Không thêm abstraction/dependency mới nếu chưa có yêu cầu cụ thể (xem `CHANGELOG.md` mục "Dự kiến" để biết cái gì cố tình chưa làm).
