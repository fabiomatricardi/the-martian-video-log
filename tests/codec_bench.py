"""Benchmark cv2.VideoWriter codecs at 1280x720 with HUD-ish frames."""
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = Path("tmp"); tmp.mkdir(exist_ok=True)
frame = np.zeros((720, 1280, 3), np.uint8)
frame[:] = (90, 60, 40)
cv2.putText(frame, "REC 00:04:32", (900, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

N = 240
for fourcc in ("mp4v", "MJPG", "XVID", "avc1"):
    ext = "mp4" if fourcc in ("mp4v", "avc1") else ("avi" if fourcc == "XVID" else "mjpg")
    path = tmp / f"bench_{fourcc}.{ext}"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), 30, (1280, 720))
    if not w.isOpened():
        print(f"{fourcc:6s}: NOT AVAILABLE")
        continue
    t0 = time.perf_counter()
    for i in range(N):
        w.write(frame)
    dt = (time.perf_counter() - t0) / N * 1000
    size = path.stat().st_size / 1024
    w.release()
    path.unlink(missing_ok=True)
    print(f"{fourcc:6s}: {dt:6.2f} ms/frame  ({1000/dt:5.1f} fps)  {size:8.0f} KB per {N} frames")
