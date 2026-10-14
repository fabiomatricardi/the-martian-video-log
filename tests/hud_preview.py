"""Render HUD preview frames (all states/palettes) + measure compose performance.

Run:  .venv/Scripts/python tests/hud_preview.py    (from project root)
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import DEFAULTS  # noqa: E402
from app.hud.overlay import HudContext, HudOverlay  # noqa: E402

OUT = ROOT / "tests" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def synthetic_frame() -> np.ndarray:
    """A fake 'selfie' scene: gradient sky, circle head, desk line."""
    h, w = 720, 1280
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        t = y / h
        frame[y, :] = (int(90 - 60 * t), int(70 + 40 * t), int(50 + 80 * t))
    cv2.circle(frame, (640, 300), 120, (140, 170, 190), -1)          # head
    cv2.ellipse(frame, (640, 480), (180, 160), 0, 180, 360, (120, 150, 170), -1)
    cv2.rectangle(frame, (0, 560), (w, h), (40, 45, 50), -1)         # desk
    cv2.putText(frame, "SYNTHETIC SCENE", (40, 620), cv2.FONT_HERSHEY_SIMPLEX,
                0.9, (180, 180, 180), 2)
    return frame


def grab_camera() -> np.ndarray | None:
    cap = cv2.VideoCapture(0, cv2.CAP_MSMF)
    frame = None
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        for _ in range(25):
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
                break
    cap.release()
    return frame


def fit_720(frame: np.ndarray) -> np.ndarray:
    h, w = 720, 1280
    fh, fw = frame.shape[:2]
    scale = min(w / fw, h / fh)
    nw, nh = int(fw * scale), int(fh * scale)
    r = cv2.resize(frame, (nw, nh))
    top, left = (h - nh) // 2, (w - nw) // 2
    out = np.zeros((h, w, 3), dtype=np.uint8)
    out[top:top + nh, left:left + nw] = r
    return out


def make_ctx(state: str, t: float = 5.4) -> HudContext:
    ctx = HudContext()
    ctx.state = state
    ctx.t_active = t
    ctx.sol = 42
    ctx.log_number = 17
    ctx.stats = {"bat": 87, "cpu": 23, "ram_gb": 4.3, "disk_gb": 210.0, "plugged": True}
    ctx.mic_level = 0.42
    ctx.transcript_lines = ["This is video log entry seventeen, sol forty-two.",
                            "and the interim tail of the sentence..."]
    return ctx


def main() -> int:
    use_camera = "--synthetic" not in sys.argv
    cam = grab_camera() if use_camera else None
    scene = fit_720(cam if cam is not None else synthetic_frame())
    src = "CAMERA" if cam is not None else "SYNTHETIC"
    print(f"frame source: {src}")
    rgb = cv2.cvtColor(scene, cv2.COLOR_BGR2RGB)
    now = datetime(2026, 10, 14, 19, 32, 5)

    shots = [
        ("recording_mission_white", "mission_white", "recording", 5.4),
        ("recording_green_phosphor", "green_phosphor", "recording", 5.4),
        ("boot_flash", "mission_white", "recording", 0.15),
        ("paused", "mission_white", "paused", 12.0),
        ("standby", "mission_white", "standby", 0.0),
    ]
    for name, pal, state, t in shots:
        cfg = {**DEFAULTS, "palette": pal}
        ov = HudOverlay(1280, 720, cfg)
        ctx = make_ctx(state, t)
        out = ov.compose(rgb, ctx, now)
        cv2.imwrite(str(OUT / f"{name}.png"), cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
        print(f"wrote {name}.png")

    if use_camera:
        # the committed README previews must be face-free: only synthetic
        # renders land in assets/img/
        print("(camera render - skipping assets/img, run with --synthetic for those)")
    else:
        img_dir = ROOT / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        for name, _pal, _state, _t in shots:
            shutil.copyfile(OUT / f"{name}.png", img_dir / f"{name}.png")
        print(f"copied {len(shots)} synthetic previews -> assets/img/")

    # performance: rolling scanlines + chromatic (worst case)
    cfg = {**DEFAULTS, "palette": "mission_white"}
    cfg["effects"]["scanline_style"] = "rolling"
    cfg["effects"]["chromatic"] = True
    ov = HudOverlay(1280, 720, cfg)
    ctx = make_ctx("recording", 5.4)
    t0 = time.perf_counter()
    n = 60
    for _ in range(n):
        ov.compose(rgb, ctx, now)
    dt = (time.perf_counter() - t0) / n * 1000.0
    print(f"compose avg: {dt:.1f} ms/frame (budget at 30fps: 33.3 ms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
