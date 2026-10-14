"""Camera worker: reads frames, burns the HUD, writes the temp MP4, emits preview.

Timeline convention: only frames captured while recording is ACTIVE (not paused)
are written, so video duration == active duration; the audio worker follows the
same rule, keeping A/V in sync across pause/resume.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from app.hud.overlay import HudContext, HudOverlay


class VideoWorker(QThread):
    preview_ready = Signal(QImage)
    camera_info = Signal(str)
    camera_failed = Signal(str)

    def __init__(self, cfg_dict: dict, ctx: HudContext, parent=None):
        super().__init__(parent)
        self._cfg = cfg_dict
        self.ctx = ctx
        self._overlay = HudOverlay(*cfg_dict.get("resolution", [1280, 720]), cfg_dict)
        self._stop = False
        self._recording = False
        self._paused = False
        self._frames = 0
        self._writer: cv2.VideoWriter | None = None
        self._temp_path: str | None = None
        self._released = threading.Event()
        self._last_preview = 0.0
        self._fps_window: list[float] = []

    # -- control ----------------------------------------------------------
    def apply_config(self, cfg_dict: dict) -> None:
        self._cfg = cfg_dict
        self._overlay = HudOverlay(*cfg_dict.get("resolution", [1280, 720]), cfg_dict)

    def start_recording(self, temp_path: str) -> bool:
        w, h = self._cfg.get("resolution", [1280, 720])
        fps = int(self._cfg.get("fps", 30))
        writer = cv2.VideoWriter(temp_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (int(w), int(h)))
        if not writer.isOpened():
            self.camera_failed.emit("VideoWriter could not open (codec missing?)")
            return False
        self._temp_path = temp_path
        self._writer = writer
        self._frames = 0
        self._paused = False
        self._recording = True
        self._released.clear()
        return True

    def pause(self, paused: bool) -> None:
        self._paused = bool(paused)

    def stop_recording(self, timeout: float = 3.0) -> dict | None:
        if not self._recording:
            return None
        self._recording = False
        self._released.wait(timeout)
        fps = int(self._cfg.get("fps", 30))
        return {
            "temp_path": self._temp_path,
            "frames": self._frames,
            "duration_s": self._frames / max(1, fps),
        }

    def shutdown(self) -> None:
        self._stop = True
        self.wait(4000)

    def measured_fps(self) -> float:
        """Effective compose+write rate over the last ~2s of processed frames."""
        win = self._fps_window
        if len(win) < 6:
            return 0.0
        span = win[-1] - win[0]
        if span <= 0:
            return 0.0
        return (len(win) - 1) / span

    # -- internals --------------------------------------------------------
    def _open_camera(self) -> cv2.VideoCapture | None:
        index = int(self._cfg.get("camera_index", 0))
        w, h = self._cfg.get("resolution", [1280, 720])
        fps = int(self._cfg.get("fps", 30))
        for backend in (cv2.CAP_MSMF, cv2.CAP_DSHOW):
            cap = cv2.VideoCapture(index, backend)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(w))
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(h))
                cap.set(cv2.CAP_PROP_FPS, fps)
                ok, _ = cap.read()
                if ok:
                    return cap
            cap.release()
        return None

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        w, h = self._cfg.get("resolution", [1280, 720])
        w, h = int(w), int(h)
        fh, fw = frame.shape[:2]
        if fw == w and fh == h:
            return frame
        scale = min(w / fw, h / fh)
        nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        top = (h - nh) // 2
        bottom = h - nh - top
        left = (w - nw) // 2
        right = w - nw - left
        return cv2.copyMakeBorder(resized, top, bottom, left, right,
                                  cv2.BORDER_CONSTANT, value=(0, 0, 0))

    def _emit_preview(self, rgb: np.ndarray) -> None:
        h, w, _ = rgb.shape
        img = QImage(rgb.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self.preview_ready.emit(img)

    def run(self) -> None:
        cap = self._open_camera()
        if cap is None:
            self.ctx.no_signal = True
            self.camera_failed.emit("Could not open camera (in use by another app?)")
        else:
            rw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            rh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            rf = cap.get(cv2.CAP_PROP_FPS)
            self.camera_info.emit(f"{rw}x{rh} @{rf:.0f}fps")

        fails = 0
        fps = max(1, int(self._cfg.get("fps", 30)))
        while not self._stop:
            if cap is not None:
                ret, frame = cap.read()
            else:
                ret, frame = False, None
            if not ret:
                fails += 1
                if fails > 45:
                    self.ctx.no_signal = True
                time.sleep(0.05)
            else:
                fails = 0
                self.ctx.no_signal = False
                rgb = cv2.cvtColor(self._fit(frame), cv2.COLOR_BGR2RGB)
                now = datetime.now()
                if self._recording and not self._paused and self._writer is not None:
                    self.ctx.t_active = self._frames / fps
                    out = self._overlay.compose(rgb, self.ctx, now)
                    self._writer.write(cv2.cvtColor(out, cv2.COLOR_RGB2BGR))
                    self._frames += 1
                else:
                    out = self._overlay.compose(rgb, self.ctx, now)
                # throttle preview: the UI needs ~15fps, the writer needs every
                # frame - flooding signals would starve this worker via the GIL
                now_mono = time.monotonic()
                self._fps_window.append(now_mono)
                if len(self._fps_window) > 120:
                    self._fps_window = self._fps_window[-120:]
                if now_mono - self._last_preview > 0.066:
                    self._last_preview = now_mono
                    self._emit_preview(out)

            if not self._recording and self._writer is not None:
                self._writer.release()
                self._writer = None
                self._released.set()
            time.sleep(0.001)

        if cap is not None:
            cap.release()
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            self._released.set()
