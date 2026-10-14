"""Raw WebSocket probe for the Live API - bypasses the google-genai SDK.

Speaks the exact documented protocol (docs: live-api/get-started-websocket,
live-api/live-transcribe) so we see every raw frame and the close reason:

  A) transcribe-live, doc-exact setup, feed AFTER setupComplete
  B) transcribe-live, doc-exact setup, feed immediately (SDK-like timing)
  C) 3.8-live with AUDIO modality (dialog fallback path)

Also prints RMS of the extracted PCM to prove the audio isn't silent.
Run:  uv run python tests/live_probe_ws.py
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

import numpy as np  # noqa: E402

from app.ai.jobs import extract_pcm16  # noqa: E402
from app.core.config import get_api_key  # noqa: E402

KEY = get_api_key()
URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
WS_URL = f"{URL}?key={KEY}"

mp4s = sorted(Path("logs").glob("*.mp4"))
pcm = extract_pcm16(mp4s[-1]) or b""
samples = np.frombuffer(pcm, dtype=np.int16)
rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0
peak = int(np.abs(samples).max()) if len(samples) else 0
print(f"pcm: {len(pcm)} bytes ({len(pcm)/32000:.1f}s)  RMS={rms:.1f}  peak={peak}")
FEED = pcm[:15 * 32000]  # 15 s
CHUNK = 3200             # 100 ms as recommended by the docs


def setup_msg(model: str, modalities: str, extra: dict | None = None) -> dict:
    s = {
        "setup": {
            "model": f"models/{model}",
            "generationConfig": {"responseModalities": [modalities]},
        }
    }
    if extra:
        s["setup"].update(extra)
    return s


def audio_frame(chunk: bytes) -> str:
    return json.dumps({"realtimeInput": {
        "audio": {"data": base64.b64encode(chunk).decode(),
                  "mimeType": "audio/pcm;rate=16000"}}})


async def raw_probe(title: str, model: str, modalities: str, extra: dict | None,
                    wait_setup: bool) -> None:
    print(f"\n=== RAW WS {title} ===")
    counts = {"frames": 0, "interim": 0, "final": 0, "output": 0}
    t0 = time.monotonic()

    def show(raw: bytes | str) -> None:
        counts["frames"] += 1
        t = time.monotonic() - t0
        try:
            m = json.loads(raw)
        except Exception:
            print(f"  {t:6.2f}s  <non-json frame, {len(raw)} bytes>")
            return
        sc = m.get("serverContent") or {}
        parts = []
        if sc.get("interimInputTranscription"):
            counts["interim"] += 1
            parts.append(f"INTERIM {sc['interimInputTranscription'].get('text', '')[:60]!r}")
        if sc.get("inputTranscription"):
            counts["final"] += 1
            parts.append(f"FINAL {sc['inputTranscription'].get('text', '')[:60]!r}")
        if sc.get("outputTranscription"):
            counts["output"] += 1
            parts.append(f"OUTPUT {sc['outputTranscription'].get('text', '')[:60]!r}")
        for k in ("setupComplete", "turnComplete", "goAway"):
            if m.get(k) is not None or sc.get(k) is not None:
                parts.append(k)
        if sc.get("modelTurn"):
            parts.append("modelTurn(audio)")
        if m.get("error"):
            parts.append(f"ERROR {json.dumps(m['error'])[:150]}")
        print(f"  {t:6.2f}s  " + (", ".join(parts) if parts else
                                  f"<frame {json.dumps(m)[:90]}>"))

    try:
        async with websockets.connect(WS_URL, max_size=20 * 1024 * 1024) as ws:
            print(f"  ws open ({time.monotonic()-t0:.2f}s)")
            await ws.send(json.dumps(setup_msg(model, modalities, extra)))
            setup_ack = asyncio.Event()

            async def reader():
                try:
                    async for raw in ws:
                        show(raw)
                        if b"setupComplete" in (raw if isinstance(raw, bytes) else
                                                raw.encode()):
                            setup_ack.set()
                except websockets.ConnectionClosed as e:
                    print(f"  reader: connection closed code={e.code} "
                          f"reason={e.reason!r}")

            rt = asyncio.create_task(reader())
            if wait_setup:
                await asyncio.wait_for(setup_ack.wait(), timeout=8.0)
                print(f"  setupComplete received, feeding ({time.monotonic()-t0:.2f}s)")
            else:
                await asyncio.sleep(0.3)  # SDK-like: feed ~immediately
            fed = 0
            while fed < len(FEED):
                await ws.send(audio_frame(FEED[fed:fed + CHUNK]))
                fed += CHUNK
                await asyncio.sleep(0.05)
            await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
            print(f"  fed {fed} bytes + stream_end ({time.monotonic()-t0:.2f}s)")
            await asyncio.sleep(5.0)  # drain finals
            with contextlib.suppress(Exception):
                await rt
    except websockets.ConnectionClosed as e:
        print(f"  CLOSED code={e.code} reason={e.reason!r}")
    except Exception as e:
        print(f"  FAIL {type(e).__name__}: {str(e)[:200]}")
    print(f"  SUMMARY {title}: frames={counts['frames']} finals={counts['final']} "
          f"interims={counts['interim']} output={counts['output']}")


async def main() -> None:
    stt_extra = {"inputAudioTranscription": {"languageCodes": []}}
    await raw_probe("A stt doc-exact, feed after setup", "gemini-3.5-transcribe-live",
                    "TEXT", stt_extra, wait_setup=True)
    await raw_probe("B stt doc-exact, feed immediately", "gemini-3.5-transcribe-live",
                    "TEXT", stt_extra, wait_setup=False)
    dialog_extra = {
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }
    await raw_probe("C 3.8-live AUDIO dialog", "gemini-3.8-live", "AUDIO",
                    dialog_extra, wait_setup=True)

asyncio.run(main())
print("\nraw probe complete.")
