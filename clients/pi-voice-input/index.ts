import { execFile } from "node:child_process";
import type {
	ExtensionAPI,
	ExtensionCommandContext,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { openMenu, type MenuItem } from "./menu";
import { AudioCapture, listWindowsAudioDevices } from "./audio-capture";
import {
	loadSettings,
	saveSettings,
	type VoiceMode,
	type WorkerProtocol,
	type WorkerState,
} from "./settings";
import { VoiceWorkerClient, type WorkerEvent } from "./worker-client";

export default function voiceInputExtension(pi: ExtensionAPI) {
	let settings = loadSettings();
	let worker = createWorker();
	let capture = createCapture();
	let state: WorkerState = "stopped";
	let ctxRef: ExtensionContext | ExtensionCommandContext | null = null;
	let listening = false;
	let activeInsertedText = "";
	let promptActivated = false;
	let waitingForSubmitPhrase = false;
	let lastVoiceDetail = "idle";
	let lastEmittedVoiceState = "";

	worker.onEvent(handleWorkerEvent);

	pi.registerShortcut("f8", {
		description: "Start or stop voice input listening",
		handler: async (ctx: ExtensionContext) => {
			ctxRef = ctx;
			await toggleListening(ctx);
		},
	});

	pi.registerCommand("voice", {
		description: "Voice input controls",
		handler: async (args: string, ctx: ExtensionCommandContext) => {
			ctxRef = ctx;
			const parts = args.trim().split(/\s+/).filter(Boolean);
			if (parts.length === 0) {
				if (ctx.hasUI)
					await openMenu(ctx, "Voice Input", buildMenuTree, (id) =>
						handleMenuAction(id, ctx),
					);
				else showStatus(ctx);
				return;
			}
			await handleCommand(parts, ctx);
		},
	});

	pi.on("session_start", async (_event, ctx) => {
		ctxRef = ctx;
		activeInsertedText = "";
		waitingForSubmitPhrase = false;

		// `/new` can preserve the extension instance, active mic capture, and worker
		// socket while swapping the Pi UI/session context. Always-mode must start a
		// fresh session behind the wake gate; otherwise the widget can say
		// "waiting for wake phrase" while the adopted worker is still awake from the
		// previous session and accepts dictation.
		if (listening) {
			if (settings.mode === "always") {
				promptActivated = false;
				lastVoiceDetail =
					"new session — rejection mode restored; waiting for wake phrase";
				worker.sendControl("reset_wake");
			} else {
				promptActivated = true;
				lastVoiceDetail = "new session — listening for transcription";
			}
			setUiState("listening");
			renderVoiceWidget(ctx, lastVoiceDetail);
			return;
		}

		promptActivated = false;
		lastVoiceDetail = "idle";
		setUiState("stopped");
		if (settings.autoLaunchWorker) startWorker(ctx, false);
		else adoptExistingWorker(ctx);
	});

	pi.on("input", async (event, ctx) => {
		if (!listening) return undefined;
		if (event.source === "extension") return undefined;

		activeInsertedText = "";
		waitingForSubmitPhrase = false;
		if (settings.mode === "always") {
			promptActivated = false;
			lastVoiceDetail =
				"keyboard prompt submitted — rejection mode restored; waiting for wake phrase";
			worker.sendControl("reset_wake");
			renderVoiceWidget(ctx, lastVoiceDetail);
		}

		return undefined;
	});

	pi.on("session_shutdown", async () => {
		// `/new` tears down the current Pi session but should not unload the
		// manually-started Python worker/model. Stop only active microphone capture
		// and detach this session from listening state; keep the worker alive so the
		// next session can adopt it without reloading Voxtral.
		stopListening({ keepWorkerAlive: true });
		ctxRef = null;
	});

	function createWorker(): VoiceWorkerClient {
		const client = new VoiceWorkerClient({
			workerHost: settings.workerHost,
			workerPort: settings.workerPort,
			workerCommand: settings.workerCommand,
			logPath: settings.logPath,
			workerProtocol: settings.workerProtocol,
			workerPath: settings.workerPath,
			websocketUrl: settings.websocketUrl,
			stripLanguageTags: settings.stripLanguageTags,
			wakePhrases: settings.wakePhrases,
			sampleRate: settings.sampleRate,
		});
		return client;
	}

	function createCapture(): AudioCapture {
		return new AudioCapture({
			sampleRate: settings.sampleRate,
			ffmpegPath: settings.ffmpegPath,
			audioDevice: settings.audioDevice,
			captureArgs: settings.captureArgs,
		});
	}

	function reloadRuntime(): void {
		stopListening();
		worker.stop();
		worker = createWorker();
		worker.onEvent(handleWorkerEvent);
		capture = createCapture();
		setUiState("stopped");
	}

	async function handleCommand(
		parts: string[],
		ctx: ExtensionCommandContext,
	): Promise<void> {
		const [cmd, ...rest] = parts;
		switch (cmd) {
			case "start-worker":
				await startWorkerFreshAndLoad(ctx);
				return;
			case "stop-worker":
				worker.stop();
				setUiState("stopped");
				return;
			case "restart-worker":
				await startWorkerFreshAndLoad(ctx);
				return;
			case "status":
				await showStatus(ctx);
				return;
			case "health":
				await showHealth(ctx);
				return;
			case "devices":
				showDevices(ctx);
				return;
			case "device": {
				const audioDevice = rest.join(" ");
				if (!audioDevice) {
					ctx.ui.notify(
						"Usage: /voice device <exact device name>. Run /voice devices first.",
						"warning",
					);
					return;
				}
				persist({ ...settings, audioDevice });
				ctx.ui.notify(
					`[voice-input] Audio device set to ${audioDevice}`,
					"info",
				);
				return;
			}
			case "listen":
				await startListening(ctx);
				return;
			case "stop":
				stopListening();
				return;
			case "download-model":
				startWorker(ctx, false);
				if (await ensureConnected(ctx)) worker.sendControl("download_model");
				return;
			case "mode": {
				const mode = rest[0] as VoiceMode | undefined;
				if (!mode || !["push-to-talk", "toggle", "always"].includes(mode)) {
					ctx.ui.notify(
						"Usage: /voice mode push-to-talk|toggle|always",
						"warning",
					);
					return;
				}
				persist({ ...settings, mode });
				ctx.ui.notify(`[voice-input] Mode set to ${mode}`, "info");
				return;
			}
			case "protocol": {
				const workerProtocol = rest[0] as WorkerProtocol | undefined;
				if (
					!workerProtocol ||
					!["tcp-jsonl", "websocket"].includes(workerProtocol)
				) {
					ctx.ui.notify(
						"Usage: /voice protocol tcp-jsonl|websocket",
						"warning",
					);
					return;
				}
				persist({ ...settings, workerProtocol });
				ctx.ui.notify(
					`[voice-input] Worker protocol set to ${workerProtocol}`,
					"info",
				);
				return;
			}
			case "websocket-url": {
				const websocketUrl = rest.join(" ");
				if (!websocketUrl || !websocketUrl.startsWith("ws")) {
					ctx.ui.notify(
						"Usage: /voice websocket-url ws://127.0.0.1:8765/ws",
						"warning",
					);
					return;
				}
				persist({ ...settings, websocketUrl, workerProtocol: "websocket" });
				ctx.ui.notify(
					`[voice-input] WebSocket URL set to ${websocketUrl}`,
					"info",
				);
				return;
			}
			case "auto-launch": {
				const value = rest[0];
				if (value !== "on" && value !== "off") {
					ctx.ui.notify("Usage: /voice auto-launch on|off", "warning");
					return;
				}
				persist({ ...settings, autoLaunchWorker: value === "on" });
				ctx.ui.notify(`[voice-input] Auto-launch ${value}`, "info");
				return;
			}
			default:
				ctx.ui.notify(`Unknown /voice command: ${cmd}`, "warning");
		}
	}

	function buildMenuTree(): MenuItem[] {
		return [
			{
				type: "action",
				id: "listen",
				label: listening ? "Stop listening" : "Start listening",
			},
			{ type: "action", id: "status", label: "Status / health ping" },
			{
				type: "submenu",
				id: "mode",
				label: `Mode: ${settings.mode}`,
				children: () =>
					(["push-to-talk", "toggle", "always"] as const).map((mode) => ({
						type: "action" as const,
						id: `mode:${mode}`,
						label: `${settings.mode === mode ? "✓ " : ""}${mode}`,
					})),
			},
			{
				type: "submenu",
				id: "worker",
				label: "Worker",
				children: () => [
					{
						type: "action",
						id: "worker:start",
						label: "Start worker (cleanup + load model)",
					},
					{ type: "action", id: "worker:stop", label: "Stop worker" },
					{
						type: "action",
						id: "worker:restart",
						label: "Restart worker (cleanup + load model)",
					},
					{
						type: "action",
						id: "worker:download",
						label: "Download model to Hugging Face cache",
					},
				],
			},
			{
				type: "submenu",
				id: "protocol",
				label: `Worker protocol: ${settings.workerProtocol}`,
				children: () =>
					(["tcp-jsonl", "websocket"] as const).map((protocol) => ({
						type: "action" as const,
						id: `protocol:${protocol}`,
						label: `${settings.workerProtocol === protocol ? "✓ " : ""}${protocol}`,
					})),
			},
			{
				type: "submenu",
				id: "transcript",
				label: `Language tags: ${settings.stripLanguageTags ? "stripped" : "kept"}`,
				children: () => [
					{
						type: "action",
						id: "langtags:strip",
						label: `${settings.stripLanguageTags ? "✓ " : ""}Strip language tags`,
					},
					{
						type: "action",
						id: "langtags:keep",
						label: `${!settings.stripLanguageTags ? "✓ " : ""}Keep language tags`,
					},
				],
			},
			{
				type: "submenu",
				id: "settings",
				label: "Settings",
				children: () => [
					{
						type: "action",
						id: "auto:on",
						label: `${settings.autoLaunchWorker ? "✓ " : ""}Auto-launch worker: on`,
					},
					{
						type: "action",
						id: "auto:off",
						label: `${!settings.autoLaunchWorker ? "✓ " : ""}Auto-launch worker: off`,
					},
					{ type: "action", id: "devices", label: "List audio devices" },
					{
						type: "submenu",
						id: "device",
						label: `Audio device: ${settings.audioDevice || "auto"}`,
						children: () => buildDeviceMenuItems(),
					},
					{
						type: "action",
						id: "wake",
						label: `Wake phrases: ${settings.wakePhrases.join(", ")}`,
					},
				],
			},
		];
	}

	async function handleMenuAction(
		id: string,
		ctx: ExtensionCommandContext,
	): Promise<void> {
		if (id === "listen") {
			await toggleListening(ctx);
			return;
		}
		if (id === "status") {
			await showStatus(ctx);
			return;
		}
		if (id.startsWith("mode:")) {
			persist({ ...settings, mode: id.slice(5) as VoiceMode });
			return;
		}
		if (id.startsWith("protocol:")) {
			persist({
				...settings,
				workerProtocol: id.slice("protocol:".length) as WorkerProtocol,
			});
			return;
		}
		if (id === "langtags:strip") {
			persist({ ...settings, stripLanguageTags: true });
			return;
		}
		if (id === "langtags:keep") {
			persist({ ...settings, stripLanguageTags: false });
			return;
		}
		if (id === "worker:start") {
			await startWorkerFreshAndLoad(ctx);
			return;
		}
		if (id === "worker:stop") {
			worker.stop();
			setUiState("stopped");
			return;
		}
		if (id === "worker:restart") {
			await startWorkerFreshAndLoad(ctx);
			return;
		}
		if (id === "worker:download") {
			startWorker(ctx, false);
			if (await ensureConnected(ctx)) worker.sendControl("download_model");
			return;
		}
		if (id === "devices") {
			showDevices(ctx);
			return;
		}
		if (id.startsWith("device:")) {
			persist({ ...settings, audioDevice: id.slice("device:".length) });
			return;
		}
		if (id === "device:auto") {
			persist({ ...settings, audioDevice: "" });
			return;
		}
		if (id === "auto:on") {
			persist({ ...settings, autoLaunchWorker: true });
			return;
		}
		if (id === "auto:off") {
			persist({ ...settings, autoLaunchWorker: false });
			return;
		}
	}

	function persist(next: typeof settings): void {
		settings = saveSettings(next);
		reloadRuntime();
	}

	function adoptExistingWorker(
		ctx: ExtensionCommandContext | ExtensionContext,
	): void {
		worker
			.connectWithRetry(1, 0)
			.then(() => worker.ping(1500))
			.then((health) => {
				const detail = `pid=${health.pid ?? "?"} model=${health.modelLoaded ? "loaded" : "not-loaded"} cuda=${health.cudaAvailable ?? "?"} ${health.cudaDevice ?? ""}`;
				setUiState("ready", detail);
				ctx.ui.setWidget("voice-input", [
					"Voice worker: ready",
					`Health: ${detail}`,
					"Listening server: open",
				]);
			})
			.catch(() => {
				worker.disconnect();
				setUiState("stopped");
			});
	}

	function startWorker(
		ctx: ExtensionCommandContext | ExtensionContext,
		notify: boolean,
	): void {
		setUiState("starting");
		try {
			worker.startNonBlocking();
			if (notify)
				ctx.ui.notify("[voice-input] Worker starting in background", "info");
		} catch (error) {
			setUiState("error", formatError(error));
			ctx.ui.notify(
				`[voice-input] Worker failed to start: ${formatError(error)}`,
				"error",
			);
		}
	}

	async function startWorkerFreshAndLoad(
		ctx: ExtensionCommandContext,
	): Promise<void> {
		try {
			await cleanupWorkers(ctx, false);
			startWorker(ctx, true);
			ctx.ui.setWidget("voice-input", [
				"Voice: worker starting · waiting for health check",
				"Input: model load will begin after the socket responds to ping",
				"Listening server: not open yet",
			]);
			const health = await waitForHealthyWorker(ctx, 120, 1000);
			if (!health) return;
			worker.sendControl("load_model");
			ctx.ui.notify(
				`[voice-input] Worker healthy (pid=${health.pid ?? "?"}); model load requested.`,
				"info",
			);
		} catch (error) {
			setUiState("error", formatError(error));
			ctx.ui.notify(
				`[voice-input] Failed to start worker: ${formatError(error)}`,
				"error",
			);
		}
	}

	async function ensureConnected(
		ctx: ExtensionCommandContext | ExtensionContext,
	): Promise<boolean> {
		try {
			await worker.connectWithRetry(40, 250);
			return true;
		} catch (error) {
			ctx.ui.notify(
				`[voice-input] Worker is not ready: ${formatError(error)}`,
				"error",
			);
			return false;
		}
	}

	async function waitForHealthyWorker(
		ctx: ExtensionCommandContext | ExtensionContext,
		attempts: number,
		delayMs: number,
	): Promise<{ pid?: number } | null> {
		for (let attempt = 1; attempt <= attempts; attempt++) {
			try {
				await worker.connectWithRetry(1, 0);
				const health = await worker.ping(1500);
				const detail = `pid=${health.pid ?? "?"} health ok`;
				setUiState("ready", detail);
				ctx.ui.setWidget("voice-input", [
					"Voice worker: ready",
					`Health: ${detail}; model=${health.modelLoaded ? "loaded" : "not-loaded"}`,
				]);
				return health;
			} catch (error) {
				if (attempt === 1 || attempt % 10 === 0) {
					ctx.ui.setWidget("voice-input", [
						`Voice: worker starting · health check ${attempt}/${attempts}`,
						`Input: waiting for socket ping (${formatError(error)})`,
						"Listening server: not open yet",
					]);
				}
				await delay(delayMs);
			}
		}
		setUiState("error", "worker health check timed out");
		ctx.ui.notify(
			"[voice-input] Worker did not pass health check; model load was not started. Check the voice worker log.",
			"error",
		);
		return null;
	}

	async function toggleListening(
		ctx: ExtensionCommandContext | ExtensionContext,
	): Promise<void> {
		if (listening) stopListening();
		else await startListening(ctx);
	}

	async function startListening(
		ctx: ExtensionCommandContext | ExtensionContext,
	): Promise<void> {
		if (listening) return;
		ctxRef = ctx;
		startWorker(ctx, false);
		if (!(await ensureConnected(ctx))) return;
		activeInsertedText = "";
		waitingForSubmitPhrase = false;
		promptActivated = settings.mode !== "always";
		lastVoiceDetail =
			settings.mode === "always"
				? `waiting for wake phrase (${settings.wakePhrases[0] ?? "wake phrase"})`
				: "listening for transcription";
		worker.sendControl("start", {
			mode: settings.mode,
			wakePhrases: settings.wakePhrases,
			sampleRate: settings.sampleRate,
		});
		listening = true;
		setUiState("listening");
		renderVoiceWidget(ctx, lastVoiceDetail);
		capture.start(
			(chunk) => {
				if (!worker.sendAudio(chunk)) {
					capture.stop();
					listening = false;
					setUiState(
						worker.isProcessRunning() ? "starting" : "stopped",
						"worker socket disconnected",
					);
					ctx.ui.notify(
						"[voice-input] Worker socket disconnected; stopped audio capture.",
						"error",
					);
				}
			},
			(message) => {
				if (
					message.includes("Immediate exit requested") ||
					message.includes("Exiting normally")
				)
					return;
				console.error(`[voice-input] capture: ${message}`);
			},
			(code) => {
				if (!listening) return;
				listening = false;
				if (worker.isConnected()) worker.sendControl("stop");
				setUiState(worker.isConnected() ? "ready" : "stopped");
				if (code !== 0 && code !== null) {
					ctx.ui.notify(
						"[voice-input] Audio capture failed. Run /voice devices and choose a microphone in /voice Settings.",
						"error",
					);
				}
			},
		);
	}

	function stopListening(options: { keepWorkerAlive?: boolean } = {}): void {
		if (!listening) return;
		capture.stop();
		if (worker.isConnected()) worker.sendControl("stop");
		listening = false;
		promptActivated = false;
		waitingForSubmitPhrase = false;
		lastVoiceDetail = "idle";
		if (!options.keepWorkerAlive)
			setUiState(worker.isConnected() ? "ready" : "stopped");
	}

	function handleWorkerEvent(event: WorkerEvent): void {
		try {
			const ctx = ctxRef;
			if (event.type === "ready") {
				const detail = `pid=${event.pid ?? "?"} cuda=${event.cudaAvailable ?? "?"} ${event.cudaDevice ?? "socket connected"}`;
				setUiState("ready", detail);
				if (ctx && !listening)
					ctx.ui.setWidget("voice-input", [
						"Voice worker: ready",
						`Health: ${detail}`,
						"Listening server: open",
					]);
				return;
			}
			if (event.type === "pong") {
				const detail = `pid=${event.pid ?? "?"} model=${event.modelLoaded ? "loaded" : "not-loaded"} cuda=${event.cudaAvailable ?? "?"} ${event.cudaDevice ?? ""}`;
				setUiState("ready", detail);
				if (ctx && !listening)
					ctx.ui.setWidget("voice-input", [
						"Voice worker: ready",
						`Health: ${detail}`,
						"Listening server: open",
					]);
				return;
			}
			if (event.type === "status") {
				setUiState(event.status as WorkerState, event.detail);
				if (event.detail === "wake gate reset") {
					promptActivated = false;
					waitingForSubmitPhrase = false;
					lastVoiceDetail =
						"prompt submitted — rejection mode restored; waiting for wake phrase";
					if (ctx && listening) renderVoiceWidget(ctx, lastVoiceDetail);
				}
				return;
			}
			if (event.type === "wake") {
				promptActivated = true;
				waitingForSubmitPhrase = false;
				lastVoiceDetail = "prompt activated — listening for transcription";
				emitVoiceState("wake", event.phrase);
				if (ctx) renderVoiceWidget(ctx, lastVoiceDetail);
				return;
			}
			if (event.type === "audio_level") {
				if (listening) {
					const marker =
						event.energy >= event.threshold
							? "hearing speech"
							: "sound below threshold";
					lastVoiceDetail = `${marker} (level ${Math.round(event.energy)} / ${Math.round(event.threshold)})`;
					if (ctx) renderVoiceWidget(ctx, lastVoiceDetail);
				}
				return;
			}
			if (event.type === "audio_accepted") {
				lastVoiceDetail = `speech accepted (${event.seconds.toFixed(1)}s), transcribing...`;
				if (ctx) renderVoiceWidget(ctx, lastVoiceDetail);
				return;
			}
			if (event.type === "audio_rejected") {
				const seconds =
					typeof event.seconds === "number"
						? `${event.seconds.toFixed(1)}s`
						: "audio";
				lastVoiceDetail = `sound rejected (${seconds}, ${event.reason}) — keep speaking clearly`;
				if (ctx) renderVoiceWidget(ctx, lastVoiceDetail);
				return;
			}
			if (event.type === "download_progress") {
				ctx?.ui.setWidget("voice-input", [
					`Voice model download: ${event.message}`,
				]);
				return;
			}
			if (event.type === "disconnected") {
				if (listening) {
					capture.stop();
					listening = false;
					promptActivated = false;
					waitingForSubmitPhrase = false;
				}
				setUiState(
					worker.isProcessRunning() ? "starting" : "stopped",
					event.reason,
				);
				return;
			}
			if (event.type === "error") {
				setUiState("error", event.message);
				ctx?.ui.notify(`[voice-input] ${event.message}`, "error");
				return;
			}
			if (event.type === "partial" || event.type === "final") {
				if (!ctx) return;
				const gatedText = gateTranscriptBeforeWake(
					ctx,
					event.text,
					event.type === "final",
				);
				if (gatedText === null) return;
				setUiState("transcribing");
				updateEditor(ctx, gatedText, event.type === "final");
				if (event.type === "final")
					setUiState(listening ? "listening" : "ready");
			}
		} catch (e) {
			// ignore stale ctx errors during session shutdown / fork
			console.error(
				"[voice-input] handleWorkerEvent error (possibly stale ctx):",
				e instanceof Error ? e.message : String(e),
			);
		}
	}

	function gateTranscriptBeforeWake(
		ctx: ExtensionCommandContext | ExtensionContext,
		text: string,
		isFinal: boolean,
	): string | null {
		if (settings.mode !== "always") return text;
		if (promptActivated) {
			const repeatedWake = matchWakePhrase(text, settings.wakePhrases);
			return repeatedWake ? repeatedWake.remainder : text;
		}

		const match = matchWakePhrase(text, settings.wakePhrases);
		if (match) {
			promptActivated = true;
			waitingForSubmitPhrase = false;
			lastVoiceDetail = "prompt activated — listening for transcription";
			emitVoiceState("wake", match.phrase);
			renderVoiceWidget(ctx, lastVoiceDetail);
			return match.remainder;
		}

		activeInsertedText = "";
		lastVoiceDetail = isFinal
			? "speech ignored — waiting for wake phrase"
			: "hearing speech — waiting for wake phrase";
		if (isFinal) worker.sendControl("reset_wake");
		if (listening) renderVoiceWidget(ctx, lastVoiceDetail);
		return null;
	}

	function wakeWordMatches(candidate: string, expected: string): boolean {
		const aliases: Record<string, string[]> = {
			hi: ["hi", "hay", "hai"],
			mei: ["mei", "may", "mai", "me", "mây", "mê"],
			meilin: [
				"meilin",
				"maylin",
				"mai linh",
				"mi linh",
				"me linh",
				"mây linh",
				"mê linh",
				"may linh",
			],
		};
		return (aliases[expected] ?? [expected]).includes(candidate);
	}

	function matchWakePhrase(
		text: string,
		phrases: string[],
	): { phrase: string; remainder: string } | null {
		const tokens = Array.from(
			text.matchAll(/[a-zA-Z0-9\u00C0-\u1EFF]+/g),
			(match) => ({
				word: match[0].toLowerCase(),
				start: match.index ?? 0,
				end: (match.index ?? 0) + match[0].length,
			}),
		);
		if (!tokens.length) return null;

		for (const phrase of phrases) {
			const phraseWords = phrase
				.toLowerCase()
				.replace(/[^a-z0-9 ]+/g, "")
				.trim()
				.split(/\s+/)
				.filter(Boolean);
			if (!phraseWords.length) continue;
			const maxStart = Math.min(
				12,
				Math.max(0, tokens.length - phraseWords.length),
			);
			for (let start = 0; start <= maxStart; start++) {
				const candidate = tokens
					.slice(start, start + phraseWords.length)
					.map((token) => token.word);
				if (
					candidate.length !== phraseWords.length ||
					candidate.some((word, i) => !wakeWordMatches(word, phraseWords[i]))
				)
					continue;
				const phraseEnd = tokens[start + phraseWords.length - 1].end;
				const remainder = text
					.slice(phraseEnd)
					.replace(/^[ \t\r\n,.!?;:\-—]+/, "")
					.trim();
				return { phrase, remainder };
			}
		}
		return null;
	}

	function updateEditor(
		ctx: ExtensionCommandContext | ExtensionContext,
		text: string,
		isFinal: boolean,
	): void {
		const nextInserted = text.trim();
		if (!nextInserted) return;
		if (isFinal) {
			const command = extractVoiceControlCommand(nextInserted);
			if (command.action === "submit") {
				if (command.content) appendVoiceText(ctx, command.content, true);
				submitEditorPrompt(ctx);
				return;
			}
			if (command.action === "stop") {
				if (command.content) appendVoiceText(ctx, command.content, true);
				stopListening();
				lastVoiceDetail = "stop command heard — voice input stopped";
				renderVoiceWidget(ctx, lastVoiceDetail);
				return;
			}
		}
		appendVoiceText(ctx, nextInserted, isFinal);
		if (
			isFinal &&
			listening &&
			(settings.mode === "toggle" || settings.mode === "always")
		) {
			waitingForSubmitPhrase = true;
			lastVoiceDetail =
				"done speaking? say 'send it', 'xong rồi nhé', or an addressed command like 'Emi run that'";
			renderVoiceWidget(ctx, lastVoiceDetail);
		}
	}

	function appendVoiceText(
		ctx: ExtensionCommandContext | ExtensionContext,
		nextInserted: string,
		isFinal: boolean,
	): void {
		const current = safeGetEditorText(ctx);
		const separator =
			current && !current.endsWith(settings.appendSeparator)
				? settings.appendSeparator
				: "";
		const nextEditorText =
			activeInsertedText && current.endsWith(activeInsertedText)
				? `${current.slice(0, -activeInsertedText.length)}${nextInserted}`
				: `${current}${separator}${nextInserted}`;
		ctx.ui.setEditorText(nextEditorText);
		activeInsertedText = isFinal ? "" : nextInserted;
		lastVoiceDetail = isFinal
			? `final appended: ${nextInserted}`
			: `transcribing: ${nextInserted}`;
		renderVoiceWidget(ctx, lastVoiceDetail);
	}

	function safeGetEditorText(
		ctx: ExtensionCommandContext | ExtensionContext,
	): string {
		try {
			return ctx.ui.getEditorText();
		} catch {
			return "";
		}
	}

	function extractVoiceControlCommand(text: string): {
		action: "none" | "submit" | "stop";
		content: string;
	} {
		const trimmed = text.trim();
		const submitPatterns = [
			/(?:^|[\s,.!?;:]+)(?:hey[\s,.!?;:]+)?(?:emi|emy|emmy|emilia)[\s,.!?;:]+(?:please[\s,.!?;:]+)?(?:send|submit)[\s,.!?;:]+(?:the[\s,.!?;:]+)?(?:prompt|message)\s*[.!?]*$/i,
			/(?:^|[\s,.!?;:]+)(?:hey[\s,.!?;:]+)?(?:emi|emy|emmy|emilia)[\s,.!?;:]+(?:please[\s,.!?;:]+)?(?:go[\s,.!?;:]+ahead|run[\s,.!?;:]+that|use[\s,.!?;:]+that|send[\s,.!?;:]+that)\s*[.!?]*$/i,
			/(?:^|[\s,.!?;:]+)(?:ok|okay)[\s,.!?;:]+send[\s,.!?;:]+it\s*[.!?]*$/i,
			/(?:^|[\s,.!?;:]+)(?:send|submit)[\s,.!?;:]+it\s*[.!?]*$/i,
			/(?:^|[\s,.!?;:]+)(?:(?:ok|okay|alright|all[\s,.!?;:]+right)[\s,.!?;:]+)?(?:that'?s|that[\s,.!?;:]+is)[\s,.!?;:]+it\s*[.!?]*$/i,
			// Vietnamese: "anh nói xong rồi" / "nói xong rồi"
			/(?:^|[\s,.!?;:]+)(?:anh[\s,.!?;:]+)?(?:nói|noi)[\s,.!?;:]+(?:xong)[\s,.!?;:]+(?:rồi|roi)\s*[.!?]*$/i,
			// Vietnamese: "mei/mai/may/mây/mê xong rồi (nhé)"
			/(?:^|[\s,.!?;:]+)(?:mei|mai|may|mây|mê)[\s,.!?;:]+(?:xong)[\s,.!?;:]+(?:rồi|roi)(?:[\s,.!?;:]+(?:nhé|nhe|nha))?\s*[.!?]*$/i,
			// Vietnamese: "xong rồi nhé" (also "ok rồi nhé")
			/(?:^|[\s,.!?;:]+)(?:xong|ok|okay)[\s,.!?;:]+(?:rồi|roi)[\s,.!?;:]+(?:nhé|nhe|nha)\s*[.!?]*$/i,
			// Vietnamese: "đủ rồi nhé"
			/(?:^|[\s,.!?;:]+)(?:đủ|du)[\s,.!?;:]+(?:rồi|roi)[\s,.!?;:]+(?:nhé|nhe|nha)\s*[.!?]*$/i,
			// Vietnamese: "hết rồi nhé"
			/(?:^|[\s,.!?;:]+)(?:hết|het)[\s,.!?;:]+(?:rồi|roi)[\s,.!?;:]+(?:nhé|nhe|nha)\s*[.!?]*$/i,
			// Vietnamese: "gửi lệnh đi em" / "gửi lệnh đi"
			/(?:^|[\s,.!?;:]+)(?:gửi|gui)[\s,.!?;:]+(?:lệnh|lenh)[\s,.!?;:]+(?:đi|di)(?:[\s,.!?;:]+(?:em|mình))?\s*[.!?]*$/i,
			// Vietnamese: "gửi đi em" / "gửi đi em con" (requires address to avoid false positives)
			/(?:^|[\s,.!?;:]+)(?:gửi|gui)[\s,.!?;:]+(?:đi|di)[\s,.!?;:]+(?:em|mình|con)(?:[\s,.!?;:]+(?:em|mình|con))*\s*[.!?]*$/i,
			// Vietnamese: "nạp câu lệnh" / "nạp câu lệnh đi em"
			/(?:^|[\s,.!?;:]+)(?:nạp|nap)[\s,.!?;:]+(?:câu|cau)[\s,.!?;:]+(?:lệnh|lenh)(?:[\s,.!?;:]+(?:đi|di)(?:[\s,.!?;:]+(?:em|mình|con)(?:[\s,.!?;:]+(?:em|mình|con))*)?)?\s*[.!?]*$/i,
			// Vietnamese: "ok xong (rồi)" / "ok xong nhé"
			/(?:^|[\s,.!?;:]+)(?:ok|okay)[\s,.!?;:]+(?:xong)(?:[\s,.!?;:]+(?:rồi|roi))?(?:[\s,.!?;:]+(?:nhé|nhe|nha))?\s*[.!?]*$/i,
			// Vietnamese: "ok gửi (đi)" / "ok gửi đi em" / "ok gửi nhé"
			/(?:^|[\s,.!?;:]+)(?:ok|okay)[\s,.!?;:]+(?:gửi|gui)(?:[\s,.!?;:]+(?:đi|di)(?:[\s,.!?;:]+(?:em|mình|con))?)?(?:[\s,.!?;:]+(?:nhé|nhe|nha))?\s*[.!?]*$/i,
		];
		for (const pattern of submitPatterns) {
			const match = pattern.exec(trimmed);
			if (match)
				return {
					action: "submit",
					content: trimmed.slice(0, match.index).trim(),
				};
		}

		const stopPatterns = [
			/^\s*(?:stop|stop listening|stop voice|voice stop)\s*[.!?]*\s*$/i,
			/(?:^|[\s,.!?;:]+)(?:hey[\s,.!?;:]+)?(?:emi|emy|emmy|emilia)[\s,.!?;:]+(?:please[\s,.!?;:]+)?(?:stop|stop listening|stop voice)\s*[.!?]*$/i,
		];
		for (const pattern of stopPatterns) {
			const match = pattern.exec(trimmed);
			if (match)
				return {
					action: "stop",
					content: trimmed.slice(0, match.index).trim(),
				};
		}

		return { action: "none", content: trimmed };
	}

	function stableEditorTextWithoutActivePartial(
		ctx: ExtensionCommandContext | ExtensionContext,
	): string {
		const current = safeGetEditorText(ctx);
		if (activeInsertedText && current.endsWith(activeInsertedText)) {
			return current.slice(0, -activeInsertedText.length).trimEnd();
		}
		return current.trimEnd();
	}

	function submitEditorPrompt(
		ctx: ExtensionCommandContext | ExtensionContext,
	): void {
		const prompt = stableEditorTextWithoutActivePartial(ctx).trim();
		activeInsertedText = "";
		if (!prompt) {
			lastVoiceDetail = "send command heard, but editor is empty";
			renderVoiceWidget(ctx, lastVoiceDetail);
			return;
		}
		ctx.ui.setEditorText("");
		waitingForSubmitPhrase = false;
		pi.events.emit("notification:force-next", {
			source: "voice-input",
			reason: "voice-submit",
		});
		pi.sendUserMessage(prompt);
		lastVoiceDetail = "send command heard — prompt submitted";
		if (settings.mode === "always") {
			promptActivated = false;
			waitingForSubmitPhrase = false;
			lastVoiceDetail =
				"prompt submitted — rejection mode restored; waiting for wake phrase";
			renderVoiceWidget(ctx, lastVoiceDetail);
			worker.sendControl("reset_wake");
			return;
		}
		renderVoiceWidget(ctx, lastVoiceDetail);
	}

	function renderVoiceWidget(
		ctx: ExtensionCommandContext | ExtensionContext,
		detail: string,
	): void {
		const gate =
			settings.mode === "always" && !promptActivated
				? `rejection mode: waiting for wake phrase (${settings.wakePhrases[0] ?? "wake phrase"})`
				: waitingForSubmitPhrase
					? "confirmation mode: waiting for send phrase"
					: "listening mode: transcription enabled";
		const active = listening
			? `active listening on (${settings.mode})`
			: "not actively listening";
		ctx.ui.setWidget("voice-input", [
			`Voice: ${active} · ${gate}`,
			`Input: ${detail}`,
			`Listening server: ${worker.isConnected() ? "open" : "not open yet"} · worker state=${state}`,
		]);
	}

	async function showStatus(ctx: ExtensionCommandContext): Promise<void> {
		const local = `mode=${settings.mode}, protocol=${settings.workerProtocol}, endpoint=${settings.websocketUrl || `${settings.workerHost}:${settings.workerPort}${settings.workerProtocol === "websocket" ? settings.workerPath : ""}`}, state=${state}, listening=${listening}, autoLaunch=${settings.autoLaunchWorker}, audioDevice=${settings.audioDevice || "auto"}, socket=${worker.isConnected()}, child=${worker.isProcessRunning()}`;
		try {
			const health = await worker.ping(2000);
			const errorLine = health.lastError
				? `\nlastError: ${health.lastError.slice(0, 1000)}`
				: "";
			ctx.ui.notify(
				`[voice-input] ${local}\nhealth: pid=${health.pid ?? "?"}, modelLoaded=${Boolean(health.modelLoaded)}, torch=${health.torchVersion || "not-loaded"}, cuda=${Boolean(health.cudaAvailable)}, device=${health.cudaDevice || "unknown"}, workerListening=${Boolean(health.listening)}, workerMode=${health.mode ?? "?"}, awake=${Boolean(health.awake)}${errorLine}`,
				health.lastError ? "warning" : "info",
			);
		} catch (error) {
			ctx.ui.notify(
				`[voice-input] ${local}\nhealth: ping failed (${formatError(error)})`,
				"warning",
			);
		}
	}

	async function showHealth(ctx: ExtensionCommandContext): Promise<void> {
		await showStatus(ctx);
	}

	async function cleanupWorkers(
		ctx: ExtensionCommandContext,
		notify = true,
	): Promise<void> {
		if (process.platform !== "win32") {
			if (notify)
				ctx.ui.notify(
					"[voice-input] Worker cleanup is currently implemented for Windows only.",
					"warning",
				);
			return;
		}
		stopListening();
		worker.disconnect();
		await new Promise<void>((resolve, reject) => {
			const script = `Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^(python|uv|uvx)') -and ($_.CommandLine -like '*sherpa_worker.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`;
			execFile(
				"powershell.exe",
				[
					"-NoProfile",
					"-NonInteractive",
					"-ExecutionPolicy",
					"Bypass",
					"-Command",
					script,
				],
				{ windowsHide: true },
				(error) => {
					if (error) reject(error);
					else resolve();
				},
			);
		}).catch((error) => {
			ctx.ui.notify(
				`[voice-input] Failed to cleanup workers: ${formatError(error)}`,
				"error",
			);
		});
		reloadRuntime();
		if (notify)
			ctx.ui.notify(
				"[voice-input] Stale voice workers cleaned up. Run /voice start-worker.",
				"info",
			);
	}

	function showDevices(ctx: ExtensionCommandContext): void {
		const devices = listWindowsAudioDevices(settings.ffmpegPath);
		if (!devices.length) {
			ctx.ui.notify(
				"[voice-input] No DirectShow audio devices found.",
				"warning",
			);
			return;
		}
		ctx.ui.notify(
			`[voice-input] Audio devices:\n${devices.map((device) => `- ${device}`).join("\n")}`,
			"info",
		);
	}

	function buildDeviceMenuItems(): MenuItem[] {
		const devices = listWindowsAudioDevices(settings.ffmpegPath);
		const items: MenuItem[] = [
			{
				type: "action",
				id: "device:auto",
				label: `${settings.audioDevice ? "" : "✓ "}Auto-detect microphone`,
			},
		];
		for (const device of devices) {
			items.push({
				type: "action",
				id: `device:${device}`,
				label: `${settings.audioDevice === device ? "✓ " : ""}${device}`,
			});
		}
		if (devices.length === 0)
			items.push({
				type: "action",
				id: "devices",
				label: "No devices found; refresh/list devices",
			});
		return items;
	}

	function setUiState(next: WorkerState, detail?: string): void {
		state = next;
		const label = detail ? `Voice: ${next} (${detail})` : `Voice: ${next}`;
		try {
			ctxRef?.ui.setStatus(
				"voice-input",
				next === "stopped" ? undefined : label,
			);
		} catch (e) {
			// ignore stale ctx errors during session shutdown / fork
		}
		emitVoiceState(next, detail);
	}

	function emitVoiceState(next: string, detail?: string): void {
		const key = `${next}:${detail ?? ""}:${listening}`;
		if (key === lastEmittedVoiceState) return;
		lastEmittedVoiceState = key;
		pi.events.emit("voice:state", {
			state: next,
			detail,
			mode: settings.mode,
			listening,
		});
	}
}

function delay(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatError(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}
