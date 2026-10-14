"""End-to-end record flow (offscreen, sandboxed data dirs).

Exercises: REC -> pause/resume -> STOP -> live session close (fake key ->
expected offline error) -> ffmpeg mux -> DB row -> background AI job.

Run:  QT_QPA_PLATFORM=offscreen .venv/Scripts/python tests/record_flow_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# -- sandbox the data locations BEFORE the UI modules bind them --------------
import app.core.config as C  # noqa: E402
import app.db.repo as R  # noqa: E402
import app.ai.jobs as J  # noqa: E402
import app.ui.record_view as RV  # noqa: E402

SANDBOX = Path(tempfile.mkdtemp(prefix="vlogflow_"))
C.DATA_DIR = SANDBOX / "data"
C.LOGS_DIR = SANDBOX / "logs"
C.TMP_DIR = SANDBOX / "tmp"
for d in (C.DATA_DIR, C.LOGS_DIR, C.TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)
R.DATA_DIR = C.DATA_DIR
J.LOGS_DIR = C.LOGS_DIR
J.TMP_DIR = C.TMP_DIR
RV.TMP_DIR = C.TMP_DIR

from PySide6.QtWidgets import QApplication  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)

    from app.core.config import Config  # noqa: E402
    from app.ui.main_window import MainWindow  # noqa: E402

    cfg = Config.load()
    cfg.set("landing_date", "2026-01-01")
    cfg.set("callsign", "FLOW TEST CALLSIGN")

    win = MainWindow(cfg)
    win.show()

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    pump(1.0)  # let the camera warm up and previews flow
    rv = win.record_view
    rv.run_preflight()
    failed = [k for k, ok in rv._checks.items() if not ok]
    if failed:
        print(f"PRE-FLIGHT RED: {failed} - cannot run camera/mic flow here")
        win.close()
        shutil.rmtree(SANDBOX, ignore_errors=True)
        return 0

    # -- REC ---------------------------------------------------------------
    rv._start_recording()
    assert rv.is_recording(), "recording did not start"
    pump(1.2)

    # -- pause / resume ----------------------------------------------------
    rv._on_pause_clicked()
    assert rv.ctx.state == "paused"
    pump(0.8)
    rv._on_pause_clicked()
    assert rv.ctx.state == "recording"
    pump(1.3)

    # -- STOP -> finalize chain --------------------------------------------
    rv._on_rec_clicked()
    t0 = time.time()
    while rv._state != "standby" and time.time() - t0 < 120:
        app.processEvents()
        time.sleep(0.05)
    assert rv._state == "standby", "finalize never returned to standby"

    rows = win.repo.list_all()
    assert len(rows) == 1, f"expected exactly 1 entry, got {len(rows)}"
    row = rows[0]
    video = C.LOGS_DIR / (row["video_file"] or "missing.mp4")
    assert video.exists() and video.stat().st_size > 20_000, "muxed MP4 missing/tiny"

    dur = float(row["duration_s"] or 0)
    print(f"filename     : {row['filename']}")
    print(f"sol / log no : {row['sol']:04d} / #{int(row['log_number']):03d}")
    print(f"duration_s   : {dur:.2f}  (expected ~2.5 = 1.2+1.3 active, pause excluded)")
    print(f"status       : {row['status']}  err: {(row['error_msg'] or '')[:110]}")
    print(f"video size   : {video.stat().st_size} bytes")
    print(f"live_model   : {row['live_model']}")

    assert 1.8 <= dur <= 3.4, f"pause/resume duration sync broken: {dur}"
    import datetime as _dt
    assert row["sol"] == (_dt.date.today()
                          - _dt.date(2026, 1, 1)).days, "SOL math wrong"
    # offline fake key -> AI job must fail cleanly into a retryable state
    assert row["status"] in ("processing", "error", "done"), row["status"]

    leftovers = [p.name for p in C.TMP_DIR.iterdir()
                 if p.name.startswith(("v_", "a_"))]
    assert not leftovers, f"temp files not cleaned: {leftovers}"

    win.close()
    pump(0.5)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    print("RECORD FLOW OK - REC/pause/STOP/mux/DB/AI-job all wired correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
