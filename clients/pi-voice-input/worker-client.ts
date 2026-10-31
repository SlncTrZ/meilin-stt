import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createConnection, type Socket } from "node:net";
import { createWriteStream, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import WebSocket from "ws";
import type { VoiceSettings, WorkerProtocol } from "./settings";

type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type WorkerEvent =
  | { type: "ready"; pid?: number; torchVersion?: string; cudaAvailable?: boolean; cudaDevice?: string }
  | { type: "pong"; id?: string; pid?: number; modelLoaded?: boolean; torchVersion?: string; cudaAvailable?: boolean; cudaDevice?: string; listening?: boolean; mode?: string; awake?: boolean; lastError?: string }
  | { type: "status"; status: string; detail?: string }
  | { type: "partial"; text: string }
  | { type: "final"; text: string }
  | { type: "wake"; phrase: string }
  | { type: "audio_level"; energy: number; threshold: number; inSpeech?: boolean }
  | { type: "audio_accepted"; seconds: number }
  | { type: "audio_rejected"; reason: string; seconds?: number; minSeconds?: number }
  | { type: "download_progress"; message: string }
  | { type: "disconnected"; reason?: string }
  | { type: "error"; message: string };

export type WorkerClientOptions = Required<Pick<VoiceSettings,
  "workerProtocol" | "workerHost" | "workerPort" | "workerPath" | "websocketUrl" | "stripLanguageTags" | "workerCommand" | "logPath" | "wakePhrases" | "sampleRate"
>>;

const ENERGY_THRESHOLD = 50.0;
const SILENCE_TIMEOUT_SECONDS = 0.8;
const MIN_SPEECH_SECONDS = 0.35;
const PRE_ROLL_SECONDS = 0.5;

export class VoiceWorkerClient {
  private child: ChildProcessWithoutNullStreams | null = null;
  private socket: Socket | null = null;
  private ws: WebSocket | null = null;
  private buffer = "";
  private listeners = new Set<(event: WorkerEvent) => void>();
  private pendingPings = new Map<string, { resolve: (event: Extract<WorkerEvent, { type: "pong" }>) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();

  private wsListening = false;
  private wsMode = "toggle";
  private wsAwake = false;
  private wsSpeechBuffer = Buffer.alloc(0);
  private wsPreRollBuffer = Buffer.alloc(0);
  private wsInSpeech = false;
  private wsLastVoiceAt = 0;
  private wsLastLevelAt = 0;
  private wsSegmentStartAt = 0;
  private wsCurrentPartial = "";
  private wsStopping = false;

  constructor(private options: WorkerClientOptions) {}

  onEvent(listener: (event: WorkerEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  isProcessRunning(): boolean {
    return this.child !== null && !this.child.killed;
  }

  isConnected(): boolean {
    if (this.isWebSocketProtocol()) return this.wsListening || this.isProcessRunning();
    return this.socket !== null && !this.socket.destroyed;
  }

  startNonBlocking(): void {
    if (this.isWebSocketProtocol()) {
      this.ping(2000)
        .then((health) => this.emit({ type: "status", status: "ready", detail: `websocket model=${health.modelLoaded ? "loaded" : "not-loaded"}` }))
        .catch(() => {
          if (this.options.workerCommand.length) this.spawnWorker();
          else this.emit({ type: "error", message: `Nemotron WebSocket server is not reachable at ${this.getWebSocketUrl()}. Start the NeMo server or set workerCommand.` });
        });
      return;
    }

    // First try to adopt an already-running compatible worker on the configured port.
    this.connectWithRetry(1, 0)
      .then(() => this.ping(1500)
        .then((health) => this.emit({ type: "status", status: "ready", detail: `pid=${health.pid ?? "?"} model=${health.modelLoaded ? "loaded" : "not-loaded"} cuda=${health.cudaAvailable ?? "?"} ${health.cudaDevice ?? ""}` }))
        .catch((error) => {
          this.disconnect();
          this.emit({ type: "error", message: `existing worker on ${this.options.workerHost}:${this.options.workerPort} did not answer health ping (${formatError(error)}). Run /voice cleanup-workers, then /voice start-worker.` });
        }))
      .catch(() => {
        this.spawnWorker();
      });
  }

  private spawnWorker(): void {
    if (this.isProcessRunning()) {
      if (!this.isConnected()) {
        this.connectWithRetry(20, 500).catch((error) => this.emit({ type: "error", message: `worker reconnect failed: ${formatError(error)}` }));
      }
      return;
    }
    mkdirSync(dirname(this.options.logPath), { recursive: true });
    const log = createWriteStream(this.options.logPath, { flags: "a" });
    const command = this.resolveWorkerCommand();
    this.child = spawn(command[0], command.slice(1), {
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
      env: {
        ...process.env,
        VOICE_INPUT_HOST: this.options.workerHost,
        VOICE_INPUT_PORT: String(this.options.workerPort),
        VOICE_INPUT_SAMPLE_RATE: String(this.options.sampleRate),
        VOICE_INPUT_WAKE_PHRASES: JSON.stringify(this.options.wakePhrases),
        VOICE_INPUT_PARENT_PID: String(process.pid),
      },
    });
    log.write(`\n[voice-input] spawned ${command.join(" ")}\n`);
    this.child.stdout.pipe(log, { end: false });
    this.child.stderr.pipe(log, { end: false });
    this.child.on("exit", (code, signal) => {
      log.write(`[voice-input] worker exited code=${code} signal=${signal}\n`);
      this.child = null;
      this.emit({ type: "status", status: "stopped", detail: `worker exited code=${code} signal=${signal}` });
    });
    this.connectWithRetry(40, 500).catch((error) => this.emit({ type: "error", message: `worker connection failed: ${formatError(error)}` }));
  }

  async connect(): Promise<void> {
    if (this.isWebSocketProtocol()) {
      await this.ping(2000);
      return;
    }
    if (this.isConnected()) return;
    await new Promise<void>((resolve, reject) => {
      const socket = createConnection({ host: this.options.workerHost, port: this.options.workerPort });
      socket.setNoDelay(true);
      socket.once("connect", () => {
        this.socket = socket;
        this.installSocketHandlers(socket);
        resolve();
      });
      socket.once("error", reject);
    });
  }

  async connectWithRetry(attempts: number, delayMs: number): Promise<void> {
    for (let i = 0; i < attempts; i++) {
      try {
        await this.connect();
        return;
      } catch {
        await delay(delayMs);
      }
    }
    throw new Error(`could not connect to ${this.options.workerHost}:${this.options.workerPort}`);
  }

  async ping(timeoutMs = 2000): Promise<Extract<WorkerEvent, { type: "pong" }>> {
    if (this.isWebSocketProtocol()) return this.pingWebSocketServer(timeoutMs);

    await this.connect();
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingPings.delete(id);
        reject(new Error("worker ping timed out"));
      }, timeoutMs);
      this.pendingPings.set(id, { resolve, reject, timer });
      if (!this.send({ type: "ping", id }, false)) {
        clearTimeout(timer);
        this.pendingPings.delete(id);
        reject(new Error("worker socket is not connected"));
      }
    });
  }

  sendAudio(chunk: Buffer): boolean {
    if (this.isWebSocketProtocol()) return this.acceptWebSocketAudio(chunk);
    return this.send({ type: "audio", data: chunk.toString("base64") }, false);
  }

  sendControl(type: string, data: Record<string, JsonValue> = {}): boolean {
    if (this.isWebSocketProtocol()) return this.sendWebSocketControl(type, data);
    return this.send({ type, ...data }, true);
  }

  disconnect(): void {
    this.socket?.destroy();
    this.socket = null;
    this.closeWebSocket(false);
  }

  stop(): void {
    for (const [id, pending] of this.pendingPings) {
      clearTimeout(pending.timer);
      pending.reject(new Error("worker stopped"));
      this.pendingPings.delete(id);
    }
    if (this.isWebSocketProtocol()) {
      this.sendWebSocketControl("stop");
    } else if (this.isConnected()) {
      this.sendControl("shutdown");
    }
    this.disconnect();
    if (this.child) {
      this.child.kill();
      this.child = null;
    }
  }

      private resolveWorkerCommand(): string[] {
        if (this.options.workerCommand.length) return this.options.workerCommand;
        if (this.isWebSocketProtocol())
          throw new Error("workerCommand is required to auto-launch a WebSocket/Nemotron server");
        throw new Error(
          "tcp-jsonl (Voxtral) worker đã bị gỡ — dùng websocket (sherpa_worker.py) hoặc tự cấu hình workerCommand",
        );
      }

  private installSocketHandlers(socket: Socket): void {
    socket.on("data", (data) => {
      this.buffer += data.toString("utf-8");
      let newline = this.buffer.indexOf("\n");
      while (newline >= 0) {
        const line = this.buffer.slice(0, newline).trim();
        this.buffer = this.buffer.slice(newline + 1);
        if (line) this.handleLine(line);
        newline = this.buffer.indexOf("\n");
      }
    });
    socket.on("close", () => {
      if (this.socket === socket) this.socket = null;
      this.emit({ type: "disconnected", reason: "socket closed" });
    });
    socket.on("error", (error) => {
      if (this.socket === socket) this.socket = null;
      this.emit({ type: "disconnected", reason: formatError(error) });
    });
  }

  private handleLine(line: string): void {
    try {
      const event = JSON.parse(line) as WorkerEvent;
      if (event.type === "pong" && event.id) {
        const pending = this.pendingPings.get(event.id);
        if (pending) {
          clearTimeout(pending.timer);
          this.pendingPings.delete(event.id);
          pending.resolve(event);
        }
      }
      this.emit(event);
    } catch (error) {
      this.emit({ type: "error", message: `invalid worker JSON: ${formatError(error)}` });
    }
  }

  private send(value: Record<string, JsonValue>, reportError: boolean): boolean {
    if (!this.socket || this.socket.destroyed) {
      if (reportError) this.emit({ type: "error", message: "worker socket is not connected" });
      return false;
    }
    return this.socket.write(`${JSON.stringify(value)}\n`);
  }

  private isWebSocketProtocol(): boolean {
    return this.options.workerProtocol === "websocket";
  }

  private getWebSocketUrl(): string {
    if (this.options.websocketUrl) return this.options.websocketUrl;
    const path = this.options.workerPath.startsWith("/") ? this.options.workerPath : `/${this.options.workerPath}`;
    return `ws://${this.options.workerHost}:${this.options.workerPort}${path}`;
  }

  private getHealthUrl(): string {
    return `http://${this.options.workerHost}:${this.options.workerPort}/health`;
  }

  private async pingWebSocketServer(timeoutMs: number): Promise<Extract<WorkerEvent, { type: "pong" }>> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(this.getHealthUrl(), { signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json().catch(() => ({})) as { model_loaded?: boolean; status?: string };
      return {
        type: "pong",
        id: randomUUID(),
        modelLoaded: Boolean(data.model_loaded),
        listening: this.wsListening,
        mode: this.wsMode,
        awake: this.wsAwake,
      };
    } finally {
      clearTimeout(timer);
    }
  }

  private sendWebSocketControl(type: string, data: Record<string, JsonValue> = {}): boolean {
    if (type === "start") {
      this.wsListening = true;
      this.wsMode = String(data.mode || "toggle");
      this.wsAwake = this.wsMode !== "always";
      this.resetWebSocketVad();
      this.emit({ type: "status", status: "listening", detail: this.wsMode });
      return true;
    }
    if (type === "stop") {
      this.wsListening = false;
      this.closeWebSocket(true);
      this.resetWebSocketVad();
      this.emit({ type: "status", status: "ready" });
      return true;
    }
    if (type === "reset_wake") {
      this.wsAwake = false;
      this.emit({ type: "status", status: "listening", detail: "wake gate reset" });
      return true;
    }
    if (type === "load_model") {
      void this.ping(2000)
        .then((health) => this.emit({ type: "ready", pid: health.pid, cudaAvailable: health.cudaAvailable, cudaDevice: health.cudaDevice }))
        .catch((error) => this.emit({ type: "error", message: `Nemotron health check failed: ${formatError(error)}` }));
      return true;
    }
    if (type === "download_model") {
      this.emit({ type: "download_progress", message: "Nemotron model download is managed by the external NeMo server startup." });
      return true;
    }
    if (type === "shutdown") {
      this.wsListening = false;
      this.closeWebSocket(false);
      return true;
    }
    return true;
  }

  private acceptWebSocketAudio(chunk: Buffer): boolean {
    if (!this.wsListening) return true;
    const energy = rmsEnergy(chunk);
    const now = Date.now() / 1000;
    if (now - this.wsLastLevelAt >= 1.0) {
      this.wsLastLevelAt = now;
      this.emit({ type: "audio_level", energy, threshold: ENERGY_THRESHOLD, inSpeech: this.wsInSpeech });
    }

    if (energy >= ENERGY_THRESHOLD) {
      if (!this.wsInSpeech) {
        this.wsInSpeech = true;
        this.wsLastVoiceAt = now;
        this.wsSegmentStartAt = now;
        this.wsSpeechBuffer = Buffer.concat([this.wsPreRollBuffer, chunk]);
        this.wsPreRollBuffer = Buffer.alloc(0);
        this.emit({ type: "status", status: "listening", detail: "speech detected" });
        this.ensureWebSocketUtterance();
        this.sendWebSocketBinary(this.wsSpeechBuffer);
        return true;
      }
      this.wsLastVoiceAt = now;
      this.wsSpeechBuffer = Buffer.concat([this.wsSpeechBuffer, chunk]);
      this.ensureWebSocketUtterance();
      this.sendWebSocketBinary(chunk);
      return true;
    }

    if (this.wsInSpeech) {
      this.wsSpeechBuffer = Buffer.concat([this.wsSpeechBuffer, chunk]);
      this.sendWebSocketBinary(chunk);
      if (now - this.wsLastVoiceAt >= SILENCE_TIMEOUT_SECONDS) {
        this.finishWebSocketUtterance(false);
      }
      return true;
    }

    this.wsPreRollBuffer = Buffer.concat([this.wsPreRollBuffer, chunk]);
    const maxPreRollBytes = Math.floor(PRE_ROLL_SECONDS * this.options.sampleRate * 2);
    if (this.wsPreRollBuffer.length > maxPreRollBytes) {
      this.wsPreRollBuffer = this.wsPreRollBuffer.subarray(this.wsPreRollBuffer.length - maxPreRollBytes);
    }
    return true;
  }

  private ensureWebSocketUtterance(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
    if (this.ws && this.ws.readyState === WebSocket.CONNECTING) return;
    this.wsStopping = false;
    this.wsCurrentPartial = "";
    const ws = new WebSocket(this.getWebSocketUrl());
    this.ws = ws;
    ws.binaryType = "nodebuffer";
    ws.on("open", () => {
      this.emit({ type: "status", status: "listening", detail: "websocket utterance open" });
    });
    ws.on("message", (data) => this.handleWebSocketMessage(data));
    ws.on("close", () => {
      if (this.ws === ws) this.ws = null;
      if (!this.wsStopping && this.wsListening) this.emit({ type: "disconnected", reason: "websocket utterance closed" });
    });
    ws.on("error", (error) => {
      if (this.ws === ws) this.ws = null;
      this.emit({ type: "error", message: `websocket error: ${formatError(error)}` });
    });
  }

  private sendWebSocketBinary(chunk: Buffer): void {
    const ws = this.ws;
    if (!ws) return;
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(chunk, { binary: true });
      return;
    }
    if (ws.readyState === WebSocket.CONNECTING) {
      ws.once("open", () => {
        if (ws.readyState === WebSocket.OPEN) ws.send(chunk, { binary: true });
      });
    }
  }

  private finishWebSocketUtterance(force: boolean): void {
    const seconds = this.wsSpeechBuffer.length / 2 / this.options.sampleRate;
    this.wsInSpeech = false;
    this.wsSpeechBuffer = Buffer.alloc(0);
    this.wsPreRollBuffer = Buffer.alloc(0);
    if (!force && seconds < MIN_SPEECH_SECONDS) {
      this.emit({ type: "audio_rejected", reason: "too_short", seconds, minSeconds: MIN_SPEECH_SECONDS });
      this.closeWebSocket(false);
      return;
    }
    this.emit({ type: "audio_accepted", seconds });
    this.emit({ type: "status", status: "transcribing", detail: "final" });
    this.closeWebSocket(true);
  }

  private closeWebSocket(sendEnd: boolean): void {
    const ws = this.ws;
    if (!ws) return;
    this.wsStopping = true;
    const closeIt = () => {
      try {
        if (sendEnd && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "end" }));
        } else if (!sendEnd && ws.readyState === WebSocket.OPEN) {
          ws.close();
        }
      } catch (error) {
        this.emit({ type: "error", message: `failed to end websocket utterance: ${formatError(error)}` });
      }
    };
    if (ws.readyState === WebSocket.CONNECTING) ws.once("open", closeIt);
    else closeIt();
  }

  private handleWebSocketMessage(data: WebSocket.RawData): void {
    let raw = "";
    if (typeof data === "string") raw = data;
    else if (Buffer.isBuffer(data)) raw = data.toString("utf-8");
    else if (Array.isArray(data)) raw = Buffer.concat(data).toString("utf-8");
    else raw = Buffer.from(data).toString("utf-8");

    try {
      const parsed = JSON.parse(raw) as { partial?: string; final?: string; error?: string };
      if (typeof parsed.error === "string") {
        this.emit({ type: "error", message: parsed.error });
        return;
      }
      if (typeof parsed.partial === "string") {
        const text = this.cleanTranscript(parsed.partial);
        this.wsCurrentPartial = text;
        this.emit({ type: "partial", text });
        return;
      }
      if (typeof parsed.final === "string") {
        const text = this.cleanTranscript(parsed.final || this.wsCurrentPartial);
        if (text) this.emit({ type: "final", text });
        this.emit({ type: "status", status: this.wsListening ? "listening" : "ready" });
        this.ws?.close();
        this.ws = null;
        return;
      }
    } catch (error) {
      this.emit({ type: "error", message: `invalid websocket JSON: ${formatError(error)} (${raw.slice(0, 200)})` });
    }
  }

  private cleanTranscript(text: string): string {
    const trimmed = text.trim();
    if (!this.options.stripLanguageTags) return trimmed;
    return trimmed.replace(/\s*<[a-z]{2}(?:-[A-Z]{2})?>\s*$/u, "").trim();
  }

  private resetWebSocketVad(): void {
    this.wsSpeechBuffer = Buffer.alloc(0);
    this.wsPreRollBuffer = Buffer.alloc(0);
    this.wsInSpeech = false;
    this.wsLastVoiceAt = 0;
    this.wsLastLevelAt = 0;
    this.wsSegmentStartAt = 0;
    this.wsCurrentPartial = "";
  }

  private emit(event: WorkerEvent): void {
    for (const listener of this.listeners) listener(event);
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function rmsEnergy(pcm: Buffer): number {
  if (pcm.length < 2) return 0;
  const samples = Math.floor(pcm.length / 2);
  if (samples === 0) return 0;
  let sumSquares = 0;
  for (let i = 0; i < samples; i++) {
    const value = pcm.readInt16LE(i * 2);
    sumSquares += value * value;
  }
  return Math.sqrt(sumSquares / samples);
}
