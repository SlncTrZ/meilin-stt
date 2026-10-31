# Voice Input Extension

Local voice input for Pi. The extension captures microphone audio, streams PCM audio to a transcription worker, appends transcript text into Pi's editor, and can submit the editor with an explicit voice command.

Supported worker protocols:

- `websocket` (default): local [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) worker with the Vietnamese model `zipformer-vi-30M` — runs on CPU, no GPU required. See [Sherpa-ONNX Local Worker](#sherpa-onnx-local-worker-recommended).
- `tcp-jsonl` (optional): bundled uv-launched Python worker using [`mistralai/Voxtral-Mini-4B-Realtime-2602`](https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602) — requires NVIDIA GPU + `uvx`.

## Install

This extension is part of **[Slnc_Pi](../../README.md)** and is installed by the parent package. From the repository root:

```bash
npm install
pi install .
```

Then restart pi or run `/reload` if pi is already running.

## Requirements

- Windows (first supported target). The default sherpa-onnx worker runs on CPU; the optional Voxtral worker needs an NVIDIA GPU.
- `uvx` available on PATH (only required for the optional Voxtral `tcp-jsonl` worker).
- Hugging Face access to `mistralai/Voxtral-Mini-4B-Realtime-2602` only for the optional Voxtral worker (`workerProtocol=tcp-jsonl`). The default `websocket` mode uses the bundled sherpa-onnx worker and auto-downloads `zipformer-vi-30M` (~33MB) on first run.
- Microphone capture uses `ffmpeg-static` by default and streams 16 kHz mono PCM audio.
- The worker/client uses an RMS voice activity threshold of `50` for speech detection.
- Voxtral native inference runs in the bundled Python worker with dependencies locked under `worker/uv.lock`.
- PyTorch is resolved from the CUDA 12.8 PyTorch wheel index (`https://download.pytorch.org/whl/cu128`) on Windows/Linux for Blackwell GPU support.

## Command

```text
/voice
```

Opens a tree menu for status, listening mode, worker control, settings, and model download. The worker start/restart actions clean stale voice workers, wait for a socket health check, then request model load.

Useful direct commands:

```text
/voice start-worker
/voice stop-worker
/voice restart-worker
/voice status
/voice health
/voice devices
/voice device <exact DirectShow audio device name>
/voice listen
/voice stop
/voice mode push-to-talk
/voice mode toggle
/voice mode always
/voice protocol tcp-jsonl
/voice protocol websocket
/voice websocket-url ws://127.0.0.1:8765/ws
/voice auto-launch on
/voice auto-launch off
/voice download-model
```

## Shortcut

```text
f8
```

Starts or stops listening using the configured `/voice mode`. `ctrl+shift+v` is avoided because many terminals reserve it for paste.

## Modes

| Mode | Behavior |
| --- | --- |
| `push-to-talk` | Starts a manual listening session from the shortcut/menu and transcribes accepted speech until stopped with `f8`, `/voice stop`, or a stop phrase. |
| `toggle` | Shortcut/menu toggles listening on and off and transcribes accepted speech. |
| `always` | Keeps listening active, but stays in rejection mode until a wake phrase is detected. Speech heard before the wake phrase is ignored; if the transcript contains a configured wake phrase or common transcription variant such as `hai mây` for `hi mei`, the wake phrase is stripped and only the remaining text is appended. After voice or keyboard submission, it resets to the wake-phrase gate. |

Default wake phrases include:

```text
hi mei, hi meilin
```

Wake matching tolerates common ASR spelling variants for these names, such as `may`/`mai`/`mây`/`mê` for `mei` and `mai linh`/`mây linh`/`mê linh` for `meilin`.

## Listening Feedback

The extension keeps a three-line `voice-input` widget visible while voice input is active:

```text
Voice: active listening on (...) · listening mode: transcription enabled
Input: hearing speech (level ... / threshold ...)
Listening server: open · worker state=...
```

In `always` mode, the first line shows whether speech is currently gated:

```text
Voice: active listening on (always) · rejection mode: waiting for wake phrase (hi mei)
```

The input line reports whether audio is below threshold, accepted for transcription, rejected as too short, currently transcribing, or ignored while always-mode is waiting for a wake phrase. When `/new` starts a fresh Pi session, always-mode resets back to the wake-phrase gate before accepting new dictated text.

## Voice Submit Commands

In `toggle` and `always` modes, each finalized speech segment is appended to the editor and the widget enters confirmation mode until you say a send phrase. If you keep speaking normally, the extension keeps appending transcript text; there is no timeout.

To submit the current editor by voice, end an utterance with a send phrase such as:

```text
send it
okay send it
submit it
that's it
okay that's it
Emi send prompt
Emmy submit the message
Hey Emilia, send the prompt
Emi go ahead
Emmy run that
Emi use that
Emi send that

Vietnamese send phrases are also supported (matched case-insensitively, with and
without diacritics, e.g. ASR output `XONG ROI NHE`):

anh nói xong rồi
nói xong rồi
mai xong rồi (also mei/may/mây/mê xong rồi)
xong rồi nhé
đủ rồi nhé
hết rồi nhé
gửi lệnh đi em
gửi lệnh đi
gửi đi em
gửi đi em con
nạp câu lệnh
nạp câu lệnh đi em
```

Short ambiguous commands such as `go ahead`, `run that`, `use that`, and `send that` require an addressed `Emi` / `Emmy` / `Emilia` prefix so normal dictated text does not accidentally submit.

If useful prompt text comes before the command, the extension keeps the useful text and omits only the command tail. For example:

```text
Please summarize the current file, Emmy run that.
```

adds/submits:

```text
Please summarize the current file
```

Natural mentions that are not submit commands, such as `I need to send the prompt to another tool`, are treated as normal transcription.

To stop listening by voice without submitting, say a standalone stop phrase such as:

```text
stop
stop listening
Emi stop listening
```

If useful text comes before an addressed stop command, the useful text is appended and only the stop command tail is omitted.

## Settings

Settings are persisted to:

```text
~/.pi/agent/voice-input.json
```

Important fields:

- `mode`: `push-to-talk`, `toggle`, or `always` (default: `toggle`).
- `autoLaunchWorker`: start the worker in the background on each Pi session.
- `ffmpegPath`: optional path to ffmpeg binary; if unset, uses `ffmpeg-static`.
- `wakePhrases`: phrases accepted by always-listening mode.
- `sampleRate`: microphone sample rate sent to the worker; default `16000`.
- `audioDevice`: Windows DirectShow audio device name. If unset, the extension auto-selects the first device containing `Microphone`, otherwise the first audio device.
- `workerProtocol`: `websocket` for the bundled sherpa-onnx worker (default), or `tcp-jsonl` for the Voxtral worker.
- `workerHost` / `workerPort`: local worker endpoint. In WebSocket mode these form `ws://<host>:<port><workerPath>` unless `websocketUrl` is set.
- `workerPath`: WebSocket path, default `/ws`.
- `websocketUrl`: full WebSocket URL override, for example `ws://127.0.0.1:8765/ws`.
- `stripLanguageTags`: remove trailing model language tags such as `<en-US>` from transcripts; default `true`.
- `workerCommand`: optional command override. Defaults to `uvx --refresh --from <extension>/worker pi-voice-worker` for `tcp-jsonl`; WebSocket mode normally expects an already-running external server unless this is configured.
- `logPath`: worker log path; default `~/.pi/agent/voice-input/voice-worker.log`.
- `captureArgs`: optional ffmpeg argument override for microphone capture.
- `appendSeparator`: text inserted between appended transcript segments (default: space).

## Local WebSocket Server Mode

Set `/voice protocol websocket` to use a separately managed local ASR server instead of the bundled `pi-voice-worker` process. By default the extension expects:

- Health check: `http://127.0.0.1:8765/health`
- Utterance stream: `ws://127.0.0.1:8765/ws`

### Sherpa-ONNX Local Worker (Recommended)

A lightweight local ASR worker using **sherpa-onnx** with Vietnamese model `zipformer-vi-30M` (33MB, runs on CPU).

**Start the worker:**

```bash
python extensions/voice-input/worker/sherpa_worker.py
```

**Auto-launch config** (`~/.pi/agent/voice-input.json`):

```json
{
  "workerProtocol": "websocket",
  "websocketUrl": "ws://127.0.0.1:8766/ws",
  "autoLaunchWorker": true,
  "workerCommand": ["python", "/path/to/sherpa_worker.py"]
}
```

The worker:

- Runs locally on CPU, no GPU required
- Supports Vietnamese speech recognition (zipformer-vi-30M)
- Listens on `ws://127.0.0.1:8766/ws`
- Health check at `http://127.0.0.1:8766/health`
- Model auto-downloads from HuggingFace on first run (~33MB)
- Uses the same VAD segmentation from the TypeScript extension

The TypeScript extension still owns microphone capture, RMS/VAD segmentation, wake-gate handling, editor updates, and voice submit/stop commands. For each detected utterance it opens a WebSocket, sends raw PCM16 LE binary frames, sends `{ "type": "end" }` when trailing silence closes the segment, and consumes JSON `{ "partial": "..." }`, `{ "final": "..." }`, or `{ "error": "..." }` messages from the server.

In this mode `/voice start-worker` only auto-launches a process when `workerCommand` is configured. Otherwise, start the local ASR server yourself and use `/voice health` or `/voice status` to verify the HTTP health endpoint. `/voice download-model` reports that model download is managed by the external server startup.

## Worker Health And Cleanup

`/voice status` and `/voice health` ping the worker socket or WebSocket-mode health endpoint and report the worker PID when available, whether the model is loaded, PyTorch version when reported, CUDA availability/device when reported, and whether the worker/server thinks it is listening.

When `autoLaunchWorker` is off, starting a new Pi chat/session does not launch a worker. If a manually started worker is already running, the extension adopts that existing worker on the new session so the loaded model stays warm across `/new`. In WebSocket mode, adoption is a `/health` check against the running ASR server.

Workers launched by the extension receive Pi's process ID and run a parent-process watchdog. If Pi exits completely, the worker shuts itself down automatically; `/new` does not trigger this because the Pi process is still alive.

`/voice start-worker` and `/voice restart-worker` automatically stop stale Windows `python`/`uv`/`uvx` processes whose command line contains `pi-voice-worker`, start a fresh worker, wait for the worker socket to answer a health ping, and only then request model load. If the health check fails, model load is not started.

## Model Download

Run:

```text
/voice download-model
```

In `websocket` mode (sherpa-onnx), model download is managed automatically by the worker on first startup. In `tcp-jsonl` mode, the worker downloads the Voxtral model into the normal Hugging Face cache.

## Emote Integration

The extension emits `voice:state` events through Pi's extension event bus. `pi-emote` listens for those events and can display a listening/transcribing indicator.

## Notes

This extension intentionally keeps model inference outside Pi's Node process. The TypeScript extension handles Pi UX and audio streaming; the Python worker handles Voxtral loading and transcription.

In `tcp-jsonl` mode, the worker uses a persistent Voxtral streaming session while speech is active. Audio frames are converted into streaming model features and fed through a queue-backed generator, while `TextIteratorStreamer` emits partial transcript updates before the VAD segment closes. A final transcript is emitted when the speech segment closes or listening stops.

In `websocket` mode, the TypeScript client performs the same RMS/VAD segmentation and opens one WebSocket utterance per detected speech segment. It sends raw PCM16 LE binary frames to `/ws`, sends `{"type":"end"}` after the VAD trailing-silence window, and maps server `partial`/`final`/`error` JSON messages into the same Pi editor flow. This mode supports the bundled sherpa-onnx worker (default) and any NeMo/Nemotron-compatible server.

The older buffered segment transcription path remains as a fallback for cases where streaming is gated or unavailable. vLLM may still be added as an alternate backend if native Transformers streaming is not fast enough.
