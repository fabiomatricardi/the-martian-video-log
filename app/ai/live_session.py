"""Gemini Live API streaming transcriber - raw WebSocket client.

Why raw websockets: the google-genai (2.30.1) live module closes sessions
right after setup when the server emits protocol frames it predates
(voiceActivity / sessionResumptionUpdate - verified Oct 2026 against the
documented protocol). This module speaks the documented BidiGenerateContent
protocol directly; google-genai is still used for the standard summary call.

Model kinds (verified against ai.google.dev Oct 2026):
  stt       gemini-3.5-transcribe-live -> TEXT modality + input transcription
  dialog    3.8 Live / 3.1 Flash Live / native audio -> AUDIO modality +
            input transcription (TEXT modality is REJECTED by dialog models)
  translate 3.5 Live Translate -> TEXT modality + system instruction;
            AUDIO+output-transcription fallback if TEXT is rejected

Sessions roll over before the API duration caps (10 min transcription,
15 min audio) so log entries of any length work. Finals/interims surface
through the LiveBridge signals; timestamps are arrival times relative to the
t0_mono passed at construction.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import threading
import time

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

DIALOG_SILENCE_INSTRUCTION = (
    "You are the silent logging assistant of a mission video-log recorder. "
    "While the user records their log, stay COMPLETELY SILENT and do not respond. "
    "Only answer if the user explicitly addresses you with a direct question, "
    "and then reply with one short sentence only."
)

# Proactive rollover points (minutes), safely below API caps (10 / 15).
SESSION_LIMIT_MIN = {"stt": 9.0, "dialog": 13.5, "translate": 13.5}

WS_BASE = ("wss://generativelanguage.googleapis.com/ws/"
           "google.ai.generativelanguage.v1beta.GenerativeService."
           "BidiGenerateContent")

SETUP_TIMEOUT_S = 8.0
STREAM_END_DRAIN_S = 4.0
RECONNECT_DELAY_S = 1.5


class LiveBridge(QObject):
    """Signals bridging the transcriber thread into the Qt world."""
    piece = Signal(str, float, bool)     # text, t_rel_seconds, is_interim
    status = Signal(str)
    failed = Signal(str)


class LiveTranscriber(threading.Thread):
    def __init__(self, api_key: str, model_id: str, kind: str, cfg_dict: dict,
                 t0_mono: float, bridge: LiveBridge | None = None,
                 input_rate: int = 16000):
        super().__init__(daemon=True, name="LiveTranscriber")
        self.api_key = api_key
        self.model_id = model_id
        self.kind = kind
        self.mode = str(cfg_dict.get("transcript_mode", "SMART"))
        self.vocab = list(cfg_dict.get("custom_vocabulary", []) or [])
        self.translate_target = str(cfg_dict.get("translate_target", "en-US"))
        self.t0 = t0_mono
        self.bridge = bridge
        self.input_rate = int(input_rate)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._aq: asyncio.Queue | None = None
        self._stop_flag = threading.Event()
        self._reconnect = threading.Event()
        self._audio_fallback = False
        self._pieces: list[tuple[float, str]] = []
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._error: str | None = None
        self._done = threading.Event()

    # -- public API (any thread) -------------------------------------------
    def feed(self, chunk: bytes) -> None:
        if self._loop is not None and self._aq is not None:
            self._loop.call_soon_threadsafe(self._aq.put_nowait, chunk)

    def finish(self, timeout: float = 15.0) -> tuple[list[tuple[float, str]], str | None]:
        self._stop_flag.set()
        if self._loop is not None and self._aq is not None:
            self._loop.call_soon_threadsafe(self._aq.put_nowait, None)
        self._done.wait(timeout)
        with self._lock:
            return list(self._pieces), self._error

    def transcript_text(self) -> str:
        with self._lock:
            return " ".join(t for _, t in self._pieces).strip()

    # -- signals -----------------------------------------------------------
    def _emit(self, text: str, t_rel: float, interim: bool) -> None:
        if self.bridge is not None:
            self.bridge.piece.emit(text, t_rel, interim)

    def _status(self, msg: str) -> None:
        if self.bridge is not None:
            self.bridge.status.emit(msg)

    def _store(self, text: str, t_rel: float) -> None:
        with self._lock:
            self._pieces.append((max(0.0, t_rel), text))
        self._emit(text, t_rel, False)

    # -- protocol ----------------------------------------------------------
    def _setup(self) -> dict:
        """BidiGenerateContent setup message (camelCase wire format)."""
        setup: dict = {"model": f"models/{self.model_id}"}
        if self.kind == "stt":
            iat: dict = {"languageCodes": []}   # [] = automatic language detection
            if self.vocab:
                iat["customVocabulary"] = self.vocab
            if self.mode:
                iat["mode"] = self.mode
            setup["generationConfig"] = {"responseModalities": ["TEXT"]}
            setup["inputAudioTranscription"] = iat
        elif self.kind == "translate":
            if self._audio_fallback:
                setup["generationConfig"] = {"responseModalities": ["AUDIO"]}
                setup["outputAudioTranscription"] = {}
            else:
                setup["generationConfig"] = {"responseModalities": ["TEXT"]}
            setup["systemInstruction"] = {"parts": [{"text":
                f"Translate the user's speech to {self.translate_target}. "
                "Output only the translation text, nothing else."}]}
        else:  # dialog: native-audio models reject TEXT - straight to AUDIO
            setup["generationConfig"] = {"responseModalities": ["AUDIO"]}
            setup["inputAudioTranscription"] = {}
            setup["outputAudioTranscription"] = {}
            setup["systemInstruction"] = {"parts": [{"text": DIALOG_SILENCE_INSTRUCTION}]}
        return {"setup": setup}

    def _audio_frame(self, chunk: bytes) -> str:
        return json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(chunk).decode("ascii"),
            "mimeType": f"audio/pcm;rate={self.input_rate}"}}})

    # -- session internals ---------------------------------------------------
    async def _wait_setup(self, ws) -> None:
        deadline = time.monotonic() + SETUP_TIMEOUT_S
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(0.05, deadline - time.monotonic()))
            m = json.loads(raw)
            if m.get("setupComplete") is not None:
                return
            if m.get("error"):
                raise RuntimeError(
                    f"setup rejected: {json.dumps(m['error'])[:200]}")
        raise TimeoutError("no setupComplete from Live server")

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            sc = m.get("serverContent") or {}
            t_rel = time.monotonic() - self.t0
            it = sc.get("interimInputTranscription")
            if it and it.get("text"):
                self._emit(str(it["text"]), t_rel, True)
            fin = sc.get("inputTranscription")
            if fin and fin.get("text") and self.kind in ("stt", "dialog"):
                self._store(str(fin["text"]), t_rel)
            out = sc.get("outputTranscription")
            if out and out.get("text") and self.kind == "translate":
                self._store(str(out["text"]), t_rel)
            if m.get("goAway") is not None:
                self._reconnect.set()

    async def _send_loop(self, ws) -> None:
        assert self._aq is not None
        while True:
            item = await self._aq.get()
            if item is None:
                break
            await ws.send(self._audio_frame(item))

    async def _main(self) -> None:
        try:
            import websockets
        except Exception as e:  # pragma: no cover
            self._error = f"websockets unavailable: {e}"
            self._done.set()
            return

        self._loop = asyncio.get_running_loop()
        self._aq = asyncio.Queue()
        url = f"{WS_BASE}?key={self.api_key}"
        limit_s = SESSION_LIMIT_MIN.get(self.kind, 13.5) * 60.0
        first_connect = True

        while not self._stop_flag.is_set() and self._error is None:
            session_started = time.monotonic()
            try:
                async with websockets.connect(url, max_size=20 * 1024 * 1024) as ws:
                    await ws.send(json.dumps(self._setup()))
                    await self._wait_setup(ws)
                    if first_connect:
                        first_connect = False
                        self._status(f"Live session ready ({self.model_id})")
                    log.info("live session up: model=%s kind=%s",
                             self.model_id, self.kind)
                    recv = asyncio.create_task(self._receive_loop(ws))
                    send = asyncio.create_task(self._send_loop(ws))
                    while True:
                        if self._stop_flag.is_set():
                            break
                        if self._reconnect.is_set():
                            self._reconnect.clear()
                            break
                        if recv.done() or send.done():
                            break  # socket died; remember why, then reconnect
                        if time.monotonic() - session_started > limit_s:
                            self._status("Live session rollover - reconnecting seamlessly")
                            break
                        await asyncio.sleep(0.25)
                    if self._stop_flag.is_set():
                        # signal end of speech, then give the server a moment to
                        # flush the final transcripts
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                ws.send(json.dumps(
                                    {"realtimeInput": {"audioStreamEnd": True}})),
                                timeout=2.0)
                        await asyncio.sleep(STREAM_END_DRAIN_S)
                    for t in (recv, send):
                        t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.gather(recv, send, return_exceptions=True)
                    for t in (recv, send):
                        if t.done() and not t.cancelled() and t.exception() is not None:
                            self._last_error = str(t.exception())[:300]
            except Exception as e:
                self._last_error = str(e)[:300]
                log.info("live session error (%s): %s",
                         self.model_id, self._last_error[:160])
                if self._stop_flag.is_set():
                    break
                if (self.kind == "translate" and not self._audio_fallback
                        and ("1007" in self._last_error
                             or "modality" in self._last_error.lower())):
                    self._audio_fallback = True
                    self._status("TEXT modality rejected - falling back to AUDIO")
                    continue
                self._status("Live session error - reconnecting")
                await asyncio.sleep(RECONNECT_DELAY_S)
        with self._lock:
            self._error = self._last_error
        self._done.set()

    # thread entry ---------------------------------------------------------
    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:  # pragma: no cover
            self._error = str(e)[:300]
        self._done.set()
