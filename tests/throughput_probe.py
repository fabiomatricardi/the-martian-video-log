"""A/B/C probe: find what slows the capture loop during real recording.
A: worker alone  B: + preview slot  C: + audio worker + live transcriber + writer
"""
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

app = QApplication(sys.argv)

from app.capture.video_worker import VideoWorker
from app.capture.audio_worker import AudioWorker
from app.ai.live_session import LiveBridge, LiveTranscriber
from app.ai.models import model_kind
from app.core.config import DEFAULTS
from app.hud.overlay import HudContext

cfg = dict(DEFAULTS)
ctx = HudContext()
w = VideoWorker(cfg, ctx)
w.start()
time.sleep(4.0)
print(f"A worker alone         : {w.measured_fps():5.1f} fps")

lbl = QLabel()
w.preview_ready.connect(lambda img: lbl.setPixmap(
    QPixmap.fromImage(img).scaled(854, 480, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.FastTransformation)))
time.sleep(4.0)
print(f"B + preview slot       : {w.measured_fps():5.1f} fps")

# full recording environment
tmp = Path("tmp"); tmp.mkdir(exist_ok=True)
aw = AudioWorker(cfg, ctx, live_feed=None)
aw.start()
time.sleep(1.0)
bridge = LiveBridge()
tr = LiveTranscriber("abc123", "gemini-3.8-live", "dialog", cfg, time.monotonic(), bridge)
tr.start()
aw.set_live_feed(tr.feed)
ctx.state = "recording"; ctx.sol = 42; ctx.log_number = 17
ok = w.start_recording(str(tmp / f"probe_{uuid.uuid4().hex}.mp4"))
aw.start_recording(str(tmp / f"probe_{uuid.uuid4().hex}.wav"))
t0 = time.monotonic()
time.sleep(4.0)
rate = w.measured_fps()
elapsed_active = time.monotonic() - t0
vres = w.stop_recording()
ares = aw.stop_recording() or {"duration_s": -1}
aw.set_live_feed(None)
tr.finish(timeout=10)
print(f"C full recording env   : {rate:5.1f} fps  (active {elapsed_active:.2f}s -> "
      f"{vres['frames']} frames = {vres['duration_s']:.2f}s timeline, "
      f"audio {ares['duration_s']:.2f}s)")
w.shutdown(); aw.shutdown()
for p in tmp.glob("probe_*.mp4"):
    p.unlink(missing_ok=True)
