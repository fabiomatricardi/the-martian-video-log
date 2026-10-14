"""Profile HudOverlay.compose cost breakdown (worst-case recording frame)."""
import cProfile
import pstats
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULTS
from app.hud.overlay import HudContext, HudOverlay

ov = HudOverlay(1280, 720, dict(DEFAULTS))
ctx = HudContext()
ctx.state = "recording"
ctx.sol = 42
ctx.log_number = 17
ctx.stats = {"bat": 87, "plugged": True, "cpu": 23, "ram_gb": 4.3, "disk_gb": 210.0}
ctx.mic_level = 0.45
ctx.transcript_lines = ["This is video log entry seventeen, sol forty-two.",
                        "and the interim tail of the sentence..."]
frame = np.zeros((720, 1280, 3), np.uint8)
frame[:] = (90, 60, 40)

wall = datetime(2026, 10, 14, 19, 32, 0)
# warm caches
for i in range(60):
    ov.compose(frame, ctx, wall + timedelta(seconds=i // 30))

pr = cProfile.Profile()
pr.enable()
t0 = time.perf_counter()
N = 300
for i in range(N):
    ov.compose(frame, ctx, wall + timedelta(seconds=i // 30))
total = (time.perf_counter() - t0) / N * 1000
pr.disable()
ps = pstats.Stats(pr, stream=sys.stdout)
ps.sort_stats("cumulative").print_stats(14)
print(f"\ncompose avg: {total:.1f} ms/frame")
