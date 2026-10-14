"""Raw WS probe round 2: validate the EXACT payloads live_session will use.

  C2) dialog AUDIO + input/output transcription + systemInstruction (silence)
  S2) stt with the app's real config (SMART + customVocabulary), longer drain
  T1) translate model, TEXT + systemInstruction

Run:  uv run python -u tests/live_probe_ws2.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sys
import time
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.jobs import extract_pcm16  # noqa: E402
from app.core.config import Config, get_api_key  # noqa: E402

KEY = get_api_key()
WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
          f"?key={KEY}")
CHUNK = 3200
cfg = Config.load().as_dict()

mp4s = sorted(Path("logs").glob("*.mp4"))
pcm = (extract_pcm16(mp4s[-1]) or b"")[:15 * 32000]
print(f"audio: {len(pcm)/32000:.1f}s")


def frames_of(title, model, setup, feed_s=15.0, drain_s=8.0, stream_end=True):
    async def run():
        counts = {"interim": 0, "final": 0, "output": 0, "text": 0, "frames": 0}
        t0 = time.monotonic()
        print(f"\n=== {title} ===")

        def show(raw):
            counts["frames"] += 1
            t = time.monotonic() - t0
            try:
                m = json.loads(raw)
            except Exception:
                print(f"  {t:6.2f}s  <bin {len(raw)}B>")
                return
            sc = m.get("serverContent") or {}
            parts = []
            it = sc.get("interimInputTranscription")
            if it and it.get("text"):
                counts["interim"] += 1
                parts.append(f"INTERIM {it['text'][:55]!r}")
            fin = sc.get("inputTranscription")
            if fin and fin.get("text"):
                counts["final"] += 1
                parts.append(f"FINAL {fin['text'][:55]!r}")
            out = sc.get("outputTranscription")
            if out and out.get("text"):
                counts["output"] += 1
                parts.append(f"OUTPUT {out['text'][:55]!r}")
            if sc.get("modelTurn"):
                parts.append("modelTurn")
            if sc.get("turnComplete"):
                parts.append("turnComplete")
            if m.get("goAway"):
                parts.append("goAway")
            if m.get("error"):
                parts.append(f"ERROR {json.dumps(m['error'])[:160]}")
            if parts:
                print(f"  {t:6.2f}s  {', '.join(parts)}")

        try:
            async with websockets.connect(WS_URL, max_size=20 * 1024 * 1024) as ws:
                await ws.send(json.dumps({"setup": setup}))
                reader = asyncio.create_task(_reader(ws, show))
                fed = 0
                await asyncio.sleep(0.5)  # let setupComplete land first
                while fed < len(pcm[:int(feed_s * 32000)]):
                    await ws.send(json.dumps({"realtimeInput": {
                        "audio": {"data": base64.b64encode(
                            pcm[fed:fed + CHUNK]).decode(),
                            "mimeType": "audio/pcm;rate=16000"}}}))
                    fed += CHUNK
                    await asyncio.sleep(0.05)
                if stream_end:
                    await ws.send(json.dumps(
                        {"realtimeInput": {"audioStreamEnd": True}}))
                print(f"  ...fed {fed}B ({time.monotonic()-t0:.1f}s), "
                      f"draining {drain_s}s...")
                await asyncio.sleep(drain_s)
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
        except Exception as e:
            print(f"  FAIL {type(e).__name__}: {str(e)[:160]}")
        print(f"  SUMMARY: frames={counts['frames']} finals={counts['final']} "
              f"interims={counts['interim']} output={counts['output']}")

    return run()


async def _reader(ws, show):
    try:
        async for raw in ws:
            show(raw)
    except websockets.ConnectionClosed as e:
        print(f"  closed code={e.code} reason={e.reason!r}")
    except asyncio.CancelledError:
        raise


VOCAB = list(cfg.get("custom_vocabulary") or [])
MODE = cfg.get("transcript_mode", "SMART")
SILENCE = ("You are the silent logging assistant of a mission video-log recorder. "
           "While the user records their log, stay COMPLETELY SILENT and do not "
           "respond. Only answer if the user explicitly addresses you with a direct "
           "question, and then reply with one short sentence only.")


async def main() -> None:
    await frames_of(
        "C2 dialog AUDIO + transcriptions + silence-instruction",
        "gemini-3.8-live",
        {"model": "models/gemini-3.8-live",
         "generationConfig": {"responseModalities": ["AUDIO"]},
         "inputAudioTranscription": {},
         "outputAudioTranscription": {},
         "systemInstruction": {"parts": [{"text": SILENCE}]}},
        stream_end=True, drain_s=6.0)

    await frames_of(
        f"S2 stt app-config (mode={MODE}, vocab={len(VOCAB)})",
        "gemini-3.5-transcribe-live",
        {"model": "models/gemini-3.5-transcribe-live",
         "generationConfig": {"responseModalities": ["TEXT"]},
         "inputAudioTranscription": {
             "languageCodes": [],
             "customVocabulary": VOCAB,
             "mode": MODE}},
        stream_end=True, drain_s=8.0)

    await frames_of(
        "T1 translate TEXT + instruction",
        "gemini-3.5-live-translate-preview",
        {"model": "models/gemini-3.5-live-translate-preview",
         "generationConfig": {"responseModalities": ["TEXT"]},
         "systemInstruction": {"parts": [{"text":
             "Translate the user's speech to en-US. Output only the translation "
             "text, nothing else."}]}},
        stream_end=True, drain_s=6.0)

asyncio.run(main())
print("\nprobe2 complete.")
