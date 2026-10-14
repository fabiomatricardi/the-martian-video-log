"""Diagnostic probe for the Gemini pipeline (uses the REAL key from .env).

  1) models.list          -> which models does THIS account actually see?
  2) chain health         -> tiny generate_content ping per summary-chain model
  3) raw Live dump        -> stream the recorded MP4's audio through
                             gemini-3.8-live (dialog cfg) and
                             gemini-3.5-transcribe-live (stt cfg),
                             printing every incoming LiveServerMessage

Run:  uv run python tests/live_probe.py
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.jobs import extract_pcm16
from app.ai.live_session import DIALOG_SILENCE_INSTRUCTION
from app.capture.muxer import get_ffmpeg_exe
from app.core.config import Config, get_api_key

CHUNK = 3200
PACE = 0.05

cfg = Config.load().as_dict()
KEY = get_api_key()
assert KEY and KEY != "abc123", "set a real GEMINI_API_KEY in .env first"
print(f"key: ...{KEY[-6:]}")

mp4s = sorted(Path("logs").glob("*.mp4"))
assert mp4s, "no recordings in logs/ yet"
mp4 = mp4s[-1]
print(f"probe audio from: {mp4.name}")

# ---------------------------------------------------------------- 1) list ---
from google import genai

client = genai.Client(api_key=KEY)
names = sorted(m.name.replace("models/", "") for m in client.models.list())
interesting = [n for n in names if ("flash" in n or "live" in n or "transcribe" in n)
               and "thinking" not in n and "tts" not in n and "image" not in n]
print(f"\n=== 1) models.list: {len(names)} total, {len(interesting)} text/live-ish ===")
for n in interesting:
    print("   ", n)

# ------------------------------------------------------------- 2) chain ----
CHAIN = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
print("\n=== 2) summary-chain health (generate_content ping) ===")
for m in CHAIN:
    try:
        t0 = time.perf_counter()
        r = client.models.generate_content(model=m, contents="Reply with the word OK only.")
        ok = (r.text or "").strip()[:20]
        print(f"    {m:32s} OK {ok!r} ({time.perf_counter()-t0:.1f}s)")
    except Exception as e:
        print(f"    {m:32s} FAIL: {str(e)[:180]}")

# ------------------------------------------------------------- 3) live -----
pcm = extract_pcm16(mp4) or b""
print(f"\npcm extracted: {len(pcm)} bytes = {len(pcm)/32000.0:.1f}s")
FEED_S = 15.0
pcm = pcm[:int(FEED_S * 32000)]

def build_config(kind: str):
    from google.genai import types
    if kind == "stt":
        return types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=[],
                custom_vocabulary=(cfg.get("custom_vocabulary") or None),
                mode=cfg.get("transcript_mode", "SMART"),
            ),
        )
    if kind == "dialog-TEXT":
        return types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            system_instruction=DIALOG_SILENCE_INSTRUCTION,
        )
    # dialog-AUDIO: the app's actual fallback path
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=DIALOG_SILENCE_INSTRUCTION,
    )


def describe(msg, t: float) -> str:
    parts = []
    sc = getattr(msg, "server_content", None)
    if sc is not None:
        for f in ("input_transcription", "interim_input_transcription",
                  "output_transcription"):
            v = getattr(sc, f, None)
            txt = getattr(v, "text", None) if v is not None else None
            if txt:
                parts.append(f"{f}={txt[:70]!r}")
        for f in ("turn_complete", "generation_complete", "interrupted"):
            if getattr(sc, f, None):
                parts.append(f)
        if getattr(sc, "go_away", None) is not None:
            parts.append("go_away")
    if getattr(msg, "text", None):
        parts.append(f"text={msg.text[:70]!r}")
    if getattr(msg, "setup_complete", None):
        parts.append("setup_complete")
    return f"  {t:6.2f}s  " + (", ".join(parts) if parts else f"<{type(msg).__name__}>")


async def live_probe(model: str, kind: str, audio: bytes) -> None:
    from google.genai import types
    print(f"\n=== 3) LIVE DUMP {model} ({kind}) ===")
    counts = {"input": 0, "interim": 0, "output": 0, "text": 0, "total": 0}
    t0 = time.monotonic()
    feed_done = asyncio.Event()

    async def sender():
        try:
            fed = 0
            while fed < len(audio):
                await sess.send_realtime_input(
                    audio=types.Blob(data=audio[fed:fed + CHUNK],
                                     mime_type="audio/pcm;rate=16000"))
                fed += CHUNK
                await asyncio.sleep(PACE)
        finally:
            feed_done.set()

    async def collector(sess):
        try:
            async for msg in sess.receive():
                counts["total"] += 1
                t = time.monotonic() - t0
                line = describe(msg, t)
                if "input_transcription=" in line and "interim" not in line.split("=")[0]:
                    counts["input"] += 1
                counts["interim"] += ("interim_input_transcription=" in line)
                counts["output"] += ("output_transcription=" in line)
                counts["text"] += (", text=" in line)
                print(line)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  RECEIVE CLOSED: {str(e)[:200]}")

    sess = None
    try:
        sess = await client.aio.live.connect(model=model, config=build_config(kind)).__aenter__()
        print(f"  connected in {time.monotonic()-t0:.2f}s")
        recv_task = asyncio.create_task(collector(sess))
        send_task = asyncio.create_task(sender())
        # feed ~7.5 s wall for 15 s of audio, then end the stream and drain
        while not feed_done.is_set() and time.monotonic() - t0 < FEED_S / 2.0 + 20:
            await asyncio.sleep(0.2)
        if send_task.done() and send_task.exception():
            raise send_task.exception()
        with contextlib.suppress(Exception):
            await sess.send_realtime_input(audio_stream_end=True)
        await asyncio.sleep(4.0)  # drain finals
    except asyncio.CancelledError:
        raise
    except BaseException as e:
        print(f"  SESSION FAIL: {type(e).__name__}: {str(e)[:200]}")
    finally:
        if sess is not None:
            with contextlib.suppress(Exception):
                await sess.__aexit__(None, None, None)
    print(f"  SUMMARY {model} [{kind}]: total_msgs={counts['total']} "
          f"input_finals={counts['input']} interims={counts['interim']} "
          f"output={counts['output']} texts={counts['text']}")


async def main3() -> None:
    await live_probe("gemini-3.8-live", "dialog-TEXT", pcm)
    await live_probe("gemini-3.8-live", "dialog-AUDIO", pcm)
    await live_probe("gemini-3.5-transcribe-live", "stt", pcm)
    await live_probe("gemini-3.1-flash-live-preview", "dialog-TEXT", pcm)

print("\nrunning live probes (about 45 s)...")
asyncio.run(main3())
print("\nprobe complete.")
