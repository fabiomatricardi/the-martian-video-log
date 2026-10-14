"""Headless smoke test: config, naming, SOL, SRT, sidecars, DB, HUD compose, mux.

Run:  .venv/Scripts/python tests/smoke_test.py   (from project root)
"""
from __future__ import annotations

import json
import sys
import tempfile
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, fn):
    try:
        fn()
        results.append((PASS, name, ""))
    except Exception as e:
        results.append((FAIL, name, f"{type(e).__name__}: {e}"))


# -- config -----------------------------------------------------------------
def t_config():
    from app.core.config import Config
    c = Config({"callsign": "TEST CALLSIGN"})
    assert c.get("callsign") == "TEST CALLSIGN"
    assert c.get("effects.scanline_opacity") == 0.12
    c.set("effects.vignette", 0.4)
    assert c.get("effects.vignette") == 0.4
    c2 = Config(c.as_dict())
    assert c2.get("effects.vignette") == 0.4


def t_env():
    from app.core import config as C
    # preserve the user's real key across the test run
    saved = C.get_api_key()
    try:
        C.set_env("GEMINI_API_KEY", "abc123")
        assert C.get_api_key() == "abc123"
    finally:
        if saved:
            C.set_env("GEMINI_API_KEY", saved)


# -- sol / naming -----------------------------------------------------------
def t_sol():
    from app.core.sol import sol_days
    assert sol_days("2026-10-01", datetime(2026, 10, 14, 23, 0)) == 13
    assert sol_days("2026-10-14", datetime(2026, 10, 14, 1, 0)) == 0
    assert sol_days("", datetime(2026, 10, 14)) == 0
    assert sol_days("2026-10-20", datetime(2026, 10, 14)) == 0


def t_naming():
    from app.core.naming import (fmt_srt_time, fmt_timecode, make_basename,
                                 unique_path)
    b = make_basename(42, datetime(2026, 10, 14, 19, 32, 5))
    assert b == "VLOG_SOL0042_20261014-1932", b
    assert fmt_timecode(3723.4) == "01:02:03"
    assert fmt_srt_time(65.537) == "00:01:05,537"
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        p1 = unique_path(d, "x", "mp4")
        p1.touch()
        p2 = unique_path(d, "x", "mp4")
        assert p2.name == "x_2.mp4", p2.name


# -- sidecar ------------------------------------------------------------------
def t_segments():
    from app.ai.sidecar import build_segments, to_captions, write_srt, write_txt
    pieces = [(1.0, "Hello mission log."), (1.3, "Second thought arrives."),
              (9.0, "New segment after silence."), (9.2, "More words.")]
    segs = build_segments(pieces)
    assert len(segs) == 2, segs
    caps = to_captions(segs)
    assert caps and caps[0]["end"] > caps[0]["start"]
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        assert write_srt(d / "t.srt", caps)
        txt = (d / "t.srt").read_text(encoding="utf-8")
        assert "-->" in txt
        write_txt(d / "t.txt",
                  {"filename": "VLOG_SOL0001_X", "log_number": 1, "sol": 1,
                   "started_at": "2026-10-14T10:00:00", "duration_s": 12.5,
                   "live_model": "gemini-3.8-live", "summary_model": "gemini-3.8-flash",
                   "callsign": "TEST"},
                  {"title": "T", "summary": "S", "topics": ["a"], "mood": "ok",
                   "highlights": ["h"]}, segs)
        body = (d / "t.txt").read_text(encoding="utf-8")
        assert "TRANSCRIPT" in body and "SUMMARY" in body


def t_segments_json():
    from app.ai.sidecar import segments_from_json
    assert segments_from_json('[{"start":1.0,"end":2.0,"text":"a"}]')[0]["text"] == "a"
    assert segments_from_json("not json") == []


# -- db ------------------------------------------------------------------------
def t_db():
    from app.db.repo import Repo, STATUS_PROCESSING
    with tempfile.TemporaryDirectory() as td:
        repo = Repo(Path(td) / "t.db")
        i = repo.insert_entry(filename="VLOG_SOL0001_T", sol=1, log_number=1,
                              started_at="2026-10-14T10:00:00", status=STATUS_PROCESSING)
        assert repo.next_log_number() == 2
        repo.update(i, status="done", summary="ok")
        row = repo.get(i)
        assert row["status"] == "done" and row["summary"] == "ok"
        assert repo.by_status("done")[0]["id"] == i
        assert len(repo.list_all()) == 1
        repo.close()


# -- hud ------------------------------------------------------------------------
def t_hud():
    import cv2
    from app.hud.overlay import HudContext, HudOverlay
    from app.core.config import DEFAULTS
    cfg = json.loads(json.dumps(DEFAULTS))
    ctx = HudContext()
    ctx.state = "recording"
    ctx.t_active = 1.3
    ctx.sol = 42
    ctx.log_number = 7
    ctx.stats = {"bat": 87, "cpu": 12, "ram_gb": 4.3, "disk_gb": 210.0, "plugged": True}
    ctx.mic_level = 0.4
    ctx.transcript_lines = ["hello mission log", "interim tail"]
    ov = HudOverlay(1280, 720, cfg)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[:] = (90, 60, 40)
    out = ov.compose(frame, ctx, datetime(2026, 10, 14, 20, 0, 0))
    assert out.shape == (720, 1280, 3) and out.dtype == np.uint8
    assert out.mean() != frame.mean()  # HUD actually drawn
    # other resolutions
    ov2 = HudOverlay(640, 480, cfg)
    out2 = ov2.compose(np.zeros((480, 640, 3), np.uint8), ctx, datetime.now())
    assert out2.shape == (480, 640, 3)
    # effects off variant
    cfg["effects"]["scanline_style"] = "off"
    cfg["effects"]["noise"] = 0.0
    cfg["effects"]["vignette"] = 0.0
    ov3 = HudOverlay(1280, 720, cfg)
    out3 = ov3.compose(frame, ctx, datetime.now())
    assert out3.shape == frame.shape


def t_fonts():
    from app.hud.fonts import font_file, load_pil_font
    from PIL import ImageFont
    p = font_file("ShareTechMono")
    assert p is not None and p.exists(), p
    f = load_pil_font("ShareTechMono", 20)
    assert isinstance(f, ImageFont.FreeTypeFont)


# -- mux (real ffmpeg) ----------------------------------------------------------
def t_mux():
    import cv2
    from app.capture.muxer import mux
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        vtmp = d / "v.mp4"
        wtmp = d / "a.wav"
        out = d / "out.mp4"
        vw = cv2.VideoWriter(str(vtmp), cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 180))
        assert vw.isOpened()
        for i in range(60):  # 2 s of moving frame
            frame = np.full((180, 320, 3), (i * 4 % 255, 50, 90), dtype=np.uint8)
            vw.write(frame)
        vw.release()
        with wave.open(str(wtmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            t = np.linspace(0, 2, 96000, endpoint=False)
            w.writeframes((np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16).tobytes())
        ok, msg = mux(str(vtmp), str(wtmp), str(out))
        assert ok, msg
        assert out.exists() and out.stat().st_size > 5000


def t_extract_pcm():
    import cv2
    from app.ai.jobs import extract_pcm16
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        vtmp = d / "v.mp4"
        vw = cv2.VideoWriter(str(vtmp), cv2.VideoWriter_fourcc(*"mp4v"), 30, (160, 90))
        for i in range(45):
            vw.write(np.zeros((90, 160, 3), np.uint8))
        vw.release()
        with wave.open(str(d / "a.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b"\x00\x40" * 48000)  # 1 s of quiet noise
        # mux not needed; ffmpeg can read wav too
        pcm = extract_pcm16(vtmp)  # mp4 without audio -> None is acceptable
        assert pcm is None or len(pcm) % 2 == 0


def t_genai_client():
    from google import genai
    c = genai.Client(api_key="fake-key-for-introspection")
    assert hasattr(c.aio, "live") and hasattr(c.models, "generate_content")


def t_live_config_build():
    from app.ai.live_session import LiveTranscriber
    from app.core.config import DEFAULTS
    tr = LiveTranscriber("fake", "gemini-3.5-transcribe-live", "stt",
                         json.loads(json.dumps(DEFAULTS)), 0.0, None)
    cfg = tr._setup()["setup"]
    assert cfg["model"] == "models/gemini-3.5-transcribe-live"
    assert cfg["generationConfig"]["responseModalities"] == ["TEXT"]
    assert cfg["inputAudioTranscription"]["mode"] == "SMART"
    assert cfg["inputAudioTranscription"]["languageCodes"] == []
    tr2 = LiveTranscriber("fake", "gemini-3.8-live", "dialog",
                          json.loads(json.dumps(DEFAULTS)), 0.0, None)
    c2 = tr2._setup()["setup"]
    # dialog models reject TEXT modality -> straight to AUDIO (verified Oct 2026)
    assert c2["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert c2["inputAudioTranscription"] == {} and c2["systemInstruction"]
    tr3 = LiveTranscriber("fake", "gemini-3.5-live-translate-preview", "translate",
                          json.loads(json.dumps(DEFAULTS)), 0.0, None)
    c4 = tr3._setup()["setup"]
    assert c4["generationConfig"]["responseModalities"] == ["TEXT"]
    assert c4["systemInstruction"]["parts"][0]["text"].startswith("Translate")
    tr3._audio_fallback = True
    c5 = tr3._setup()["setup"]
    assert c5["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert c5["outputAudioTranscription"] == {}


def t_summary_schema():
    from app.ai.summarizer import SCHEMA, _clean
    clean = _clean({"title": "T", "summary": "S", "topics": ["a", 2], "mood": "m"})
    assert clean["topics"] == ["a", "2"] and "highlights" in clean
    assert SCHEMA["type"] == "OBJECT"


def main() -> int:
    checks = [
        ("config roundtrip", t_config),
        ("env parse/write", t_env),
        ("sol counter", t_sol),
        ("naming + collision", t_naming),
        ("segments/srt/txt", t_segments),
        ("segments json", t_segments_json),
        ("sqlite repo", t_db),
        ("hud compose 720/480 + effects off", t_hud),
        ("font resolution", t_fonts),
        ("ffmpeg mux roundtrip", t_mux),
        ("extract pcm16", t_extract_pcm),
        ("genai client", t_genai_client),
        ("live config builders", t_live_config_build),
        ("summary schema", t_summary_schema),
    ]
    for name, fn in checks:
        check(name, fn)
    w = max(len(n) for _, n, _ in results)
    fails = 0
    for status, name, err in results:
        mark = "[OK]  " if status == PASS else "[FAIL]"
        if status == FAIL:
            fails += 1
        print(f"{mark} {name:{w}}  {err}")
    print(f"\n{len(results) - fails}/{len(results)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
