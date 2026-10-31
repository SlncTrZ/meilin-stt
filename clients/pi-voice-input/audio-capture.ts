import { spawn, spawnSync, type ChildProcessByStdio } from "node:child_process";
import type { Readable } from "node:stream";
import { createRequire } from "node:module";
import type { VoiceSettings } from "./settings";

export type AudioCaptureOptions = Required<Pick<VoiceSettings, "sampleRate" | "ffmpegPath" | "audioDevice" | "captureArgs">>;

export class AudioCapture {
  private child: ChildProcessByStdio<null, Readable, Readable> | null = null;

  constructor(private options: AudioCaptureOptions) {}

  isRunning(): boolean {
    return this.child !== null && !this.child.killed;
  }

  start(onChunk: (chunk: Buffer) => void, onError: (message: string) => void, onExit?: (code: number | null, signal: NodeJS.Signals | null) => void): void {
    if (this.isRunning()) return;
    const command = this.resolveFfmpegPath();
    const args = this.options.captureArgs.length ? this.options.captureArgs : defaultCaptureArgs(command, this.options.sampleRate, this.options.audioDevice);
    const child = spawn(command, args, { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    this.child = child;
    child.stdout.on("data", (chunk: Buffer) => onChunk(chunk));
    child.stderr.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf-8").trim();
      if (text) onError(text);
    });
    child.on("exit", (code, signal) => {
      this.child = null;
      if (code !== 0 && code !== null) onError(`audio capture exited code=${code} signal=${signal}`);
      onExit?.(code, signal);
    });
  }

  stop(): void {
    if (!this.child) return;
    this.child.kill();
    this.child = null;
  }

  private resolveFfmpegPath(): string {
    if (this.options.ffmpegPath) return this.options.ffmpegPath;
    try {
      const require = createRequire(import.meta.url);
      const ffmpegStatic = require("ffmpeg-static") as string | null;
      if (ffmpegStatic) return ffmpegStatic;
    } catch {
      // fall back to PATH
    }
    return "ffmpeg";
  }
}

export function listWindowsAudioDevices(ffmpegPath?: string): string[] {
  if (process.platform !== "win32") return [];
  const command = ffmpegPath || resolveDefaultFfmpegPath();
  const result = spawnSync(command, ["-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"], { encoding: "utf-8" });
  return parseDshowDevices(`${result.stdout ?? ""}\n${result.stderr ?? ""}`);
}

function parseDshowDevices(output: string): string[] {
  const devices: string[] = [];
  for (const line of output.split(/\r?\n/)) {
    const match = line.match(/\] "(.+)" \(audio\)/);
    if (match) devices.push(match[1]);
  }
  return devices;
}

function defaultCaptureArgs(ffmpegPath: string, sampleRate: number, audioDevice: string): string[] {
  if (process.platform === "win32") {
    const device = audioDevice || chooseWindowsAudioDevice(ffmpegPath);
    return ["-hide_banner", "-loglevel", "error", "-f", "dshow", "-i", `audio=${device}`, "-ac", "1", "-ar", String(sampleRate), "-f", "s16le", "pipe:1"];
  }
  if (process.platform === "darwin") {
    return ["-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", ":0", "-ac", "1", "-ar", String(sampleRate), "-f", "s16le", "pipe:1"];
  }
  return ["-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", "default", "-ac", "1", "-ar", String(sampleRate), "-f", "s16le", "pipe:1"];
}

function chooseWindowsAudioDevice(ffmpegPath: string): string {
  const devices = listWindowsAudioDevices(ffmpegPath);
  return devices.find((device) => /microphone/i.test(device)) ?? devices[0] ?? "default";
}

function resolveDefaultFfmpegPath(): string {
  try {
    const require = createRequire(import.meta.url);
    const ffmpegStatic = require("ffmpeg-static") as string | null;
    if (ffmpegStatic) return ffmpegStatic;
  } catch {
    // fall back to PATH
  }
  return "ffmpeg";
}
