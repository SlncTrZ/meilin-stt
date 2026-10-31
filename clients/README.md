# Clients

## `cli.py`

Client tham chiếu, không phụ thuộc package này (chỉ cần `httpx`, và `sounddevice`/`websockets` cho `mic`/`stream`):

```bash
python clients/cli.py ping
python clients/cli.py transcribe ./meeting.m4a
python clients/cli.py stream ./cau3s.wav            # giả lập client WS như Pi
python clients/cli.py mic                            # thu micro → WS → final
python clients/cli.py feedback "mở Qdrant ra" --raw "mở quảng chạy ra" --id <utterance_id>
```

Trỏ sang máy khác: `--http http://<host>:8766 --ws ws://<host>:8766/ws`.

## `pi-voice-input/`

**Bản sao tham chiếu** của extension Pi (`extensions/voice-input` của repo `SlncTrZ/Slnc_Pi`), đặt ở đây để:
- một máy mới có đủ "cả bộ" client để đọc/đối chiếu,
- xem chính xác giao thức mà Pi đang dùng.

⚠️ **Không phải bản để sửa.** Muốn đổi hành vi extension (ví dụ thêm hook gửi `/v1/feedback`) thì sửa trong repo `Slnc_Pi`, rồi copy lại đây. Cách trỏ extension sang service này: xem [docs/migration-from-pi.md](../docs/migration-from-pi.md).
