"""Client CLI cho meilin-stt — kiểm tra API trước khi cắm vào Pi/agent khác.

Wing: code | Topic: stt-client | Updated: 2026-10-31 23:07

Ví dụ:
  python clients/cli.py ping
  python clients/cli.py transcribe ./sample.wav
  python clients/cli.py mic                    # thu từ micro (cần sounddevice)
  python clients/cli.py feedback "Xin chào Qdrant" --raw "xin chào quảng chạy"
  python clients/cli.py stream ./sample.wav    # giả lập client WS (PCM16, 100ms/frame)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path

import httpx

DEFAULT_HTTP = "http://127.0.0.1:8766"
DEFAULT_WS = "ws://127.0.0.1:8766/ws"


def cmd_ping(args: argparse.Namespace) -> int:
    resp = httpx.get(f"{args.http}/health", timeout=10)
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
    return 0 if resp.status_code == 200 else 1


def cmd_transcribe(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"Không thấy file: {path}", file=sys.stderr)
        return 2
    with path.open("rb") as fh:
        resp = httpx.post(
            f"{args.http}/v1/transcribe",
            files={"file": (path.name, fh, "application/octet-stream")},
            params={"apply_corrections": str(args.correct).lower(), "segment": "true"},
            timeout=1800,
        )
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1
    data = resp.json()
    print(data["text"])
    print(
        f"\n-- {data['duration_sec']}s audio, {data['elapsed_sec']}s xử lý, "
        f"RTF {data['rtf']}, provider {data['provider']}, {len(data['segments'])} đoạn",
        file=sys.stderr,
    )
    return 0


def _read_wav_pcm16(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit("Cần wav PCM16 (16-bit). Convert: ffmpeg -i in.wav -ac 1 -ar 16000 -f wav out.wav")
        return wav.readframes(wav.getnframes()), wav.getframerate()


async def _stream_file(args: argparse.Namespace) -> int:
    import websockets

    pcm, rate = _read_wav_pcm16(Path(args.file))
    if rate != 16000:
        print(f"⚠️  wav là {rate}Hz — server resample tuyến tính, nên convert về 16kHz", file=sys.stderr)
    url = f"{args.ws}?lane=interactive&sample_rate={rate}"
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        print(f"→ {url}")
        ready = json.loads(await ws.recv())
        print(f"← {ready}")
        frame = int(0.1 * rate) * 2  # 100ms
        for i in range(0, len(pcm), frame):
            await ws.send(pcm[i : i + frame])
            await asyncio.sleep(0.01)  # không cần realtime, chỉ để partial kịp trả
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.01))
                print(f"← {json.dumps(message, ensure_ascii=False)}")
            except TimeoutError:
                continue
        await ws.send(json.dumps({"type": "end"}))
        while True:
            message = await ws.recv()
            data = json.loads(message)
            print(f"← {json.dumps(data, ensure_ascii=False)}")
            if "final" in data or "error" in data:
                break
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    return asyncio.run(_stream_file(args))


def cmd_mic(args: argparse.Namespace) -> int:
    """Thu từ micro tới khi im lặng, gửi qua WS (cần sounddevice)."""
    try:
        import numpy as np
        import sounddevice as sd
        import websockets
    except ImportError as exc:
        print(f"Thiếu dependency ({exc}). Cài: pip install sounddevice websockets numpy", file=sys.stderr)
        return 2

    rate = 16000
    block = int(0.05 * rate)
    threshold = args.threshold
    silence_blocks_needed = int(args.silence / 0.05)

    async def run() -> int:
        async with websockets.connect(f"{args.ws}?lane=interactive&sample_rate={rate}") as ws:
            print("← " + await ws.recv())
            print("🎤 Nói đi Anh (Ctrl+C để dừng)...")
            silence = 0
            started = False
            with sd.RawInputStream(samplerate=rate, channels=1, dtype="int16", blocksize=block) as stream:
                while True:
                    buf, _ = stream.read(block)
                    pcm = bytes(buf)
                    rms = float(np.sqrt(np.mean(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) ** 2)))
                    if rms > threshold * 32768:
                        started = True
                        silence = 0
                    elif started:
                        silence += 1
                    if started:
                        await ws.send(pcm)
                    if started and silence >= silence_blocks_needed:
                        break
            await ws.send(json.dumps({"type": "end"}))
            while True:
                data = json.loads(await ws.recv())
                if "partial" in data:
                    print(f"\r… {data['partial']}", end="", flush=True)
                    continue
                print(f"\n✅ {data.get('final') or data.get('error')}")
                if data.get("utterance_id"):
                    print(f"   utterance_id={data['utterance_id']} (dùng cho feedback)")
                break
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


def cmd_feedback(args: argparse.Namespace) -> int:
    payload = {
        "utterance_id": args.id or "",
        "corrected_text": args.corrected,
        "raw_text": args.raw or "",
        "source": "cli",
    }
    resp = httpx.post(f"{args.http}/v1/feedback", json=payload, timeout=30)
    print(resp.status_code, resp.text)
    return 0 if resp.status_code == 200 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meilin-stt-client", description=__doc__)
    parser.add_argument("--http", default=DEFAULT_HTTP, help=f"base URL HTTP (mặc định {DEFAULT_HTTP})")
    parser.add_argument("--ws", default=DEFAULT_WS, help=f"URL WS (mặc định {DEFAULT_WS})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ping = sub.add_parser("ping", help="GET /health")
    p_ping.set_defaults(func=cmd_ping)

    p_tr = sub.add_parser("transcribe", help="transcribe file qua /v1/transcribe")
    p_tr.add_argument("file")
    p_tr.add_argument("--no-correct", dest="correct", action="store_false")
    p_tr.set_defaults(func=cmd_transcribe, correct=True)

    p_stream = sub.add_parser("stream", help="gửi wav qua WS như client thật")
    p_stream.add_argument("file")
    p_stream.set_defaults(func=cmd_stream)

    p_mic = sub.add_parser("mic", help="thu micro rồi gửi WS")
    p_mic.add_argument("--threshold", type=float, default=0.01)
    p_mic.add_argument("--silence", type=float, default=0.8, help="giây im lặng để chốt")
    p_mic.set_defaults(func=cmd_mic)

    p_fb = sub.add_parser("feedback", help="gửi feedback sửa lỗi (nguồn học)")
    p_fb.add_argument("corrected")
    p_fb.add_argument("--raw", default="")
    p_fb.add_argument("--id", default="")
    p_fb.set_defaults(func=cmd_feedback)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
