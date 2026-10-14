"""Record screen: live HUD preview, REC/PAUSE/STOP, model quick-select, preflight."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from app.ai.jobs import JobRunner
from app.ai.live_session import LiveBridge, LiveTranscriber
from app.ai.models import LIVE_MODELS, model_kind
from app.ai.sidecar import build_segments
from app.capture.audio_worker import AudioWorker
from app.capture.muxer import get_ffmpeg_exe, mux
from app.capture.video_worker import VideoWorker
from app.core.config import PROJECT_ROOT, TMP_DIR, get_api_key
from app.core.naming import make_basename, unique_path
from app.core.sol import sol_days
from app.core.stats import snapshot
from app.db.repo import STATUS_DONE, STATUS_ERROR, STATUS_PROCESSING, Repo
from app.hud.overlay import HudContext


class _LiveStop(QThread):
    finished_with = Signal(list, str)   # pieces [[t, text], ...], error

    def __init__(self, transcriber: LiveTranscriber):
        super().__init__()
        self._tr = transcriber

    def run(self) -> None:
        pieces, err = self._tr.finish(timeout=25.0)
        self.finished_with.emit(pieces, err or "")


class _MuxThread(QThread):
    done = Signal(bool, str)

    def __init__(self, video_tmp: str, wav_tmp: str, out_path: str):
        super().__init__()
        self._args = (video_tmp, wav_tmp, out_path)

    def run(self) -> None:
        ok, msg = mux(*self._args)
        self.done.emit(ok, msg)


class RecordView(QWidget):
    go_library = Signal()

    def __init__(self, cfg, repo: Repo, jobs: JobRunner,
                 video_worker: VideoWorker, audio_worker: AudioWorker,
                 ctx: HudContext, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.repo = repo
        self.jobs = jobs
        self.video_worker = video_worker
        self.audio_worker = audio_worker
        self.ctx = ctx

        self._state = "standby"          # standby | recording | finalizing
        self._paused = False
        self._rec: dict = {}
        self._finals: list[tuple[float, str]] = []
        self._interim = ""
        self._live: LiveTranscriber | None = None
        self._live_stop: _LiveStop | None = None
        self._mux: _MuxThread | None = None
        self._discard = False
        self._checks: dict[str, bool] = {}
        self._ffmpeg_ok: bool | None = None

        self._build_ui()
        self._wire()

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(2000)
        self._stats_timer.timeout.connect(self._stats_tick)
        self._stats_timer.start()

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        self.preview = QLabel()
        self.preview.setObjectName("previewLabel")
        self.preview.setMinimumSize(854, 480)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#05070a; border:1px solid #1d2430;")

        self.btn_rec = QPushButton("REC  -  START LOG")
        self.btn_rec.setObjectName("recBtn")
        self.btn_pause = QPushButton("PAUSE")
        self.btn_pause.setObjectName("pauseBtn")
        self.btn_pause.setEnabled(False)
        self.btn_library = QPushButton("LIBRARY")

        self.combo_model = QComboBox()
        for m in LIVE_MODELS:
            self.combo_model.addItem(m["label"], m["id"])
        idx = self.combo_model.findData(str(self.cfg.get("live_model", "")))
        if idx >= 0:
            self.combo_model.setCurrentIndex(idx)

        self.lbl_live = QLabel("LIVE TRANSCRIPT: idle")
        self.lbl_live.setProperty("hud", True)
        self.lbl_last = QLabel("No entries yet - your first log awaits, commander.")
        self.lbl_last.setProperty("hud", True)
        self.lbl_preflight = QLabel("")
        self.lbl_preflight.setProperty("hud", True)

        controls = QHBoxLayout()
        controls.addWidget(self.btn_rec)
        controls.addWidget(self.btn_pause)
        controls.addWidget(QLabel("Live model:"))
        controls.addWidget(self.combo_model, 1)
        controls.addWidget(self.btn_library)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        lay.addWidget(self.preview, 1)
        lay.addWidget(self.lbl_preflight)
        lay.addWidget(self.lbl_live)
        lay.addWidget(self.lbl_last)
        lay.addLayout(controls)

    def _wire(self) -> None:
        self.video_worker.preview_ready.connect(self._on_preview)
        self.video_worker.camera_info.connect(self._on_camera_info)
        self.video_worker.camera_failed.connect(self._on_camera_failed)
        self.audio_worker.error.connect(self._on_audio_error)
        self.btn_rec.clicked.connect(self._on_rec_clicked)
        self.btn_pause.clicked.connect(self._on_pause_clicked)
        self.btn_library.clicked.connect(self.go_library.emit)
        self.combo_model.currentIndexChanged.connect(self._on_model_changed)

    # -- preflight ----------------------------------------------------------
    def run_preflight(self) -> dict[str, bool]:
        checks: dict[str, bool] = {}
        if self._ffmpeg_ok is None:
            try:
                get_ffmpeg_exe()
                self._ffmpeg_ok = True
            except Exception:
                self._ffmpeg_ok = False
        checks["FFMPEG"] = bool(self._ffmpeg_ok)
        checks["KEY"] = bool(get_api_key())
        try:
            checks["DISK"] = shutil.disk_usage(str(PROJECT_ROOT)).free > (1024 ** 3)
        except Exception:
            checks["DISK"] = False
        checks["CAM"] = bool(self._camera_ok())
        checks["MIC"] = bool(self._mic_ok())
        self._checks = checks
        dots = "   ".join(
            f"<span style='color:{'#39d98a' if ok else '#e2543e'}'>"
            f"{'●' if ok else '×'} {name}</span>" for name, ok in checks.items()
        )
        self.lbl_preflight.setText("PRE-FLIGHT:  " + dots)
        return checks

    def _camera_ok(self) -> bool:
        return getattr(self, "_cam_state", True) and not self.ctx.no_signal

    def _mic_ok(self) -> bool:
        return getattr(self, "_mic_state", True)

    # -- slots ---------------------------------------------------------------
    def _on_preview(self, img: QImage) -> None:
        self._cam_seen = True
        pm = QPixmap.fromImage(img)
        self.preview.setPixmap(
            pm.scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.FastTransformation))

    def _on_camera_info(self, info: str) -> None:
        self._cam_state = True
        self.lbl_last.setText(f"CAMERA: {info}")

    def _on_camera_failed(self, msg: str) -> None:
        self._cam_state = False
        self.lbl_last.setText(f"CAMERA PROBLEM: {msg}")

    def _on_audio_error(self, msg: str) -> None:
        if msg:
            self._mic_state = False
            self.lbl_last.setText(f"MIC PROBLEM: {msg}")
        else:
            self._mic_state = True

    def _on_model_changed(self, _idx: int) -> None:
        mid = self.combo_model.currentData()
        if mid:
            self.cfg.set("live_model", mid)
            self.cfg.save()

    def _stats_tick(self) -> None:
        self.ctx.stats = snapshot()
        if self._state == "standby":
            self.run_preflight()
        elif self._state == "recording":
            fps = self.video_worker.measured_fps()
            self.lbl_preflight.setText(f"CAPTURE: {fps:.1f} fps effective (target {self.cfg.get('fps', 30)})")

    def _on_live_piece(self, text: str, _t: float, interim: bool) -> None:
        if interim:
            self._interim = text
        else:
            # precise timestamps arrive with transcriber.finish(); store text only
            self._finals.append((0.0, text))
        joined = " ".join(t for _, t in self._finals)
        line1 = ("..." + joined[-100:]) if len(joined) > 100 else joined
        self.ctx.transcript_lines = [line for line in (line1, self._interim) if line]

    def _on_live_status(self, msg: str) -> None:
        self.lbl_live.setText(f"LIVE TRANSCRIPT: {msg}")

    # -- recording control ----------------------------------------------------
    def _on_rec_clicked(self) -> None:
        if self._state == "standby":
            self._start_recording()
        elif self._state == "recording":
            self._stop_recording()

    def _start_recording(self) -> None:
        checks = self.run_preflight()
        bad = [k for k, ok in checks.items() if not ok]
        if bad:
            QMessageBox.warning(self, "Pre-flight check failed",
                                "Cannot start: " + ", ".join(bad) +
                                "\n\nFix the red items (see status line) and try again.")
            return
        started = datetime.now()
        sol = sol_days(str(self.cfg.get("landing_date", "")), started)
        log_number = self.repo.next_log_number()
        fps = int(self.cfg.get("fps", 30))

        ctx = self.ctx
        ctx.state = "recording"
        ctx.t_active = 0.0
        ctx.sol = sol
        ctx.log_number = log_number
        ctx.transcript_lines = []
        self._finals = []
        self._interim = ""

        temp_v = TMP_DIR / f"v_{uuid.uuid4().hex}.mp4"
        temp_w = TMP_DIR / f"a_{uuid.uuid4().hex}.wav"
        if not self.video_worker.start_recording(str(temp_v)):
            ctx.state = "standby"
            return
        self.audio_worker.start_recording(str(temp_w))

        self._rec = {
            "started": started, "sol": sol, "log": log_number, "fps": fps,
            "temp_v": temp_v, "temp_w": temp_w,
            "live_model": str(self.cfg.get("live_model", "")),
        }
        self._discard = False

        key = get_api_key()
        if key:
            bridge = LiveBridge()
            bridge.piece.connect(self._on_live_piece)
            bridge.status.connect(self._on_live_status)
            self._live_t0 = time.monotonic()
            self._live = LiveTranscriber(
                key, self._rec["live_model"], model_kind(self._rec["live_model"]),
                self.cfg.as_dict(), self._live_t0, bridge,
                input_rate=self.audio_worker.live_output_rate())
            self._live.start()
            self.audio_worker.set_live_feed(self._live.feed)
            self.lbl_live.setText("LIVE TRANSCRIPT: connecting...")
        else:
            self._live = None
            self.audio_worker.set_live_feed(None)
            self.lbl_live.setText("LIVE TRANSCRIPT: off (no API key)")

        self._state = "recording"
        self._paused = False
        self._sync_buttons()
        self.run_preflight()

    def _on_pause_clicked(self) -> None:
        if self._state != "recording":
            return
        self._paused = not self._paused
        self.video_worker.pause(self._paused)
        self.audio_worker.pause(self._paused)
        self.ctx.state = "paused" if self._paused else "recording"
        self._sync_buttons()

    def _stop_recording(self) -> None:
        if self._state != "recording":
            return
        self._state = "finalizing"
        self._sync_buttons()
        self.lbl_last.setText("FINALIZING - closing live session and encoding MP4...")

        vres = self.video_worker.stop_recording()
        ares = self.audio_worker.stop_recording()
        self.audio_worker.set_live_feed(None)
        self._vres = vres or {"temp_path": None, "frames": 0, "duration_s": 0.0}
        self._ares = ares or {"wav_path": None, "samples": 0, "duration_s": 0.0}
        self.ctx.state = "standby"

        if vres is None or vres["frames"] < int(self._rec.get("fps", 30)):
            self._discard = True  # too short -> discard, but still close live cleanly

        if self._live is not None:
            self._live_stop = _LiveStop(self._live)
            self._live_stop.finished_with.connect(self._on_live_stopped)
            self._live_stop.start()
            self._live = None
        else:
            self._on_live_stopped([], "")

    def _on_live_stopped(self, pieces: list, err: str) -> None:
        self._live_stop = None
        if self._discard:
            self._discard = False
            self._cleanup_temps()
            self.lbl_live.setText("LIVE TRANSCRIPT: idle")
            self.lbl_last.setText("Log discarded (under one second) - nothing recorded.")
            self._state = "standby"
            self._sync_buttons()
            return
        if err:
            self.lbl_live.setText(f"LIVE TRANSCRIPT: ended with error ({err[:80]})")

        pieces = [(float(t), str(x)) for t, x in pieces]
        segments = build_segments(pieces)
        transcript = " ".join(s["text"] for s in segments).strip()
        self._pending = {"segments": segments, "transcript": transcript,
                         "live_err": err}

        base = make_basename(self._rec["sol"], self._rec["started"])
        final = unique_path(self.app_logs_dir(), base, "mp4")
        self._final_base = final.stem
        self._final_name = final.name

        self._mux = _MuxThread(str(self._rec["temp_v"]), str(self._rec["temp_w"]),
                               str(final))
        self._mux.done.connect(self._on_mux_done)
        self._mux.start()

    def _on_mux_done(self, ok: bool, msg: str) -> None:
        self._mux = None
        if not ok:
            self._cleanup_temps(keep_broken=True)
            QMessageBox.critical(self, "Encoding failed",
                                 "The recording could not be encoded:\n" + msg +
                                 "\n\nTemp files kept in tmp/ for recovery.")
            self._state = "standby"
            self._sync_buttons()
            return
        self._cleanup_temps()

        rec = self._rec
        pending = getattr(self, "_pending", {"segments": [], "transcript": "", "live_err": ""})
        entry_id = self.repo.insert_entry(
            filename=self._final_base,
            sol=rec["sol"],
            log_number=rec["log"],
            started_at=rec["started"].isoformat(timespec="seconds"),
            ended_at=datetime.now().isoformat(timespec="seconds"),
            duration_s=round(float(self._vres["duration_s"]), 2),
            video_file=self._final_name,
            transcript=pending["transcript"],
            segments=json.dumps(pending["segments"], ensure_ascii=False),
            status=STATUS_PROCESSING,
            error_msg=(pending["live_err"] or "")[:400],
            live_model=rec["live_model"],
        )
        self.jobs.enqueue(entry_id)
        self.lbl_last.setText(
            f"ENTRY #{rec['log']:03d} - {self._final_base} - summarizing in background...")
        self.lbl_live.setText("LIVE TRANSCRIPT: idle")
        self._state = "standby"
        self._sync_buttons()

    # -- helpers --------------------------------------------------------------
    def app_logs_dir(self):
        from app.core.config import LOGS_DIR
        return LOGS_DIR

    def _cleanup_temps(self, keep_broken: bool = False) -> None:
        tv, tw = self._rec.get("temp_v"), self._rec.get("temp_w")
        if keep_broken:
            try:
                if tv and tv.exists():
                    tv.rename(TMP_DIR / f"broken_{tv.name}")
                if tw and tw.exists():
                    tw.rename(TMP_DIR / f"broken_{tw.name}")
            except Exception:
                pass
        else:
            for p in (tv, tw):
                try:
                    if p and p.exists():
                        p.unlink()
                except Exception:
                    pass

    def _sync_buttons(self) -> None:
        recording = self._state == "recording"
        finalizing = self._state == "finalizing"
        self.btn_rec.setText("STOP  -  END LOG" if recording else
                             ("WORKING..." if finalizing else "REC  -  START LOG"))
        self.btn_rec.setProperty("recording", "true" if recording else "false")
        self.btn_rec.style().unpolish(self.btn_rec)
        self.btn_rec.style().polish(self.btn_rec)
        self.btn_pause.setEnabled(recording)
        self.btn_pause.setText("RESUME" if self._paused else "PAUSE")
        self.combo_model.setEnabled(self._state == "standby")

    def is_recording(self) -> bool:
        return self._state == "recording"
