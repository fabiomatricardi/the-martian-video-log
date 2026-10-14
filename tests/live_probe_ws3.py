"""Raw WS probe round 3: why does gemini-3.5-transcribe-live stall?

Matrix: {realtime, 2x} pace x {VERBATIM-minimal, SMART+vocab} x {stream_end, VAD-only}
Run:  uv run python -u tests/live_probe_ws3.py
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
VOCAB = list(cfg.get("custom_vocabulary") or [])

mp4s = sorted(Path("logs").glob("*.mp4"))
pcm = (extract_pcm16(mp4s[-1]) or b"")[:15 * 32000]
print(f"audio: {len(pcm)/32000:.1f}s")


async def _reader(ws, show):
    try:
        async for raw in ws:
            show(raw)
    except websockets.ConnectionClosed as e:
        print(f"  closed code={e.code} reason={e.reason!r}")
    except asyncio.CancelledError:
        raise


def case(title, iat, pace_s, stream_end, drain_s, feed_s=15.0):
    async def run():
        counts = {"interim": 0, "final": 0, "frames": 0, "chars": 0}
        t0 = time.monotonic()
        print(f"\n=== {title} ===")

        def show(raw):
            counts["frames"] += 1
            t = time.monotonic() - t0
            try:
                m = json.loads(raw)
            except Exception:
                return
            sc = m.get("serverContent") or {}
            it = sc.get("interimInputTranscription")
            fin = sc.get("inputTranscription")
            if it and it.get("text"):
                counts["interim"] += 1
                counts["chars"] += len(it["text"])
                print(f"  {t:6.2f}s  INTERIM {it['text'][:60]!r}")
            if fin and fin.get("text"):
                counts["final"] += 1
                counts["chars"] += len(fin["text"])
                print(f"  {t:6.2f}s  FINAL   {fin['text'][:60]!r}")

        try:
            async with websockets.connect(WS_URL, max_size=20 * 1024 * 1024) as ws:
                await ws.send(json.dumps({"setup": {
                    "model": "models/gemini-3.5-transcribe-live",
                    "generationConfig": {"responseModalities": ["TEXT"]},
                    "inputAudioTranscription": iat}}))
                reader = asyncio.create_task(_reader(ws, show))
                await asyncio.sleep(0.5)
                fed = 0
                target = len(pcm[:int(feed_s * 32000)])
                while fed < target:
                    await ws.send(json.dumps({"realtimeInput": {
                        "audio": {"data": base64.b64encode(
                            pcm[fed:fed + CHUNK]).decode(),
                            "mimeType": "audio/pcm;rate=16000"}}}))
                    fed += CHUNK
                    await asyncio.sleep(pace_s)
                if stream_end:
                    await ws.send(json.dumps(
                        {"realtimeInput": {"audioStreamEnd": True}}))
                    print(f"  ...fed {fed}B, stream_end sent "
                          f"({time.monotonic()-t0:.1f}s)...")
                else:
                    print(f"  ...fed {fed}B, waiting for server VAD "
                          f"({time.monotonic()-t0:.1f}s)...")
                await asyncio.sleep(drain_s)
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
        except Exception as e:
            print(f"  FAIL {type(e).__name__}: {str(e)[:160]}")
        print(f"  SUMMARY: frames={counts['frames']} finals={counts['final']} "
              f"interims={counts['interim']} total_chars={counts['chars']}")

    return run()


async def main() -> None:
    await case("S3 VERBATIM-minimal, REALTIME pace, VAD-only (no stream_end)",
               {"languageCodes": []}, 0.10, False, 12.0)
    await case("S4 SMART+vocab, REALTIME pace, stream_end",
               {"languageCodes": [], "customVocabulary": VOCAB, "mode": "SMART"},
               0.10, True, 10.0)
    await case("S5 SMART+vocab, 2x pace, stream_end",
               {"languageCodes": [], "customVocabulary": VOCAB, "mode": "SMART"},
               0.05, True, 10.0)

asyncio.run(main())
print("\nprobe3 complete.")
