"""Settings dialog: identity, AI models, capture, HUD palettes/effects, API key."""
from __future__ import annotations

import shutil
from datetime import date

from PySide6.QtCore import Qt, QDate, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog,
                               QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QSlider, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from app.ai.models import LIVE_MODELS, SUMMARY_CHAIN
from app.core.config import PROJECT_ROOT, get_api_key, set_env

try:
    import sounddevice as sd
except Exception:  # pragma: no cover
    sd = None

RESOLUTIONS = [(1280, 720), (1920, 1080), (640, 480)]
FONTS = ["ShareTechMono", "Michroma", "Orbitron"]
PALETTES = ["mission_white", "green_phosphor", "ares_blue"]


class _ModelTest(QThread):
    done = Signal(bool, str)   # ok, message

    def __init__(self, model_id: str):
        super().__init__()
        self._model = model_id

    def run(self):
        try:
            from app.ai.summarizer import available_models
            key = get_api_key()
            if not key:
                self.done.emit(False, "GEMINI_API_KEY not set")
                return
            names = available_models(key)
            if self._model in names:
                self.done.emit(True, f"'{self._model}' is available ({len(names)} models visible)")
            else:
                self.done.emit(False, f"'{self._model}' not in your model list "
                                      f"({len(names)} visible). Check spelling/quota page.")
        except Exception as e:
            self.done.emit(False, f"API test failed: {e}")


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Settings - Mission Config")
        self.setMinimumWidth(560)
        self._test_thread = None

        tabs = QTabWidget()

        # ---- Mission tab -------------------------------------------------
        mission = QWidget()
        mf = QFormLayout(mission)
        self.callsign = QLineEdit(str(cfg.get("callsign", "")))
        self.landing = QDateEdit()
        self.landing.setCalendarPopup(True)
        self.landing.setDisplayFormat("yyyy-MM-dd")
        try:
            y, m, d = [int(x) for x in str(cfg.get("landing_date", "")).split("-")]
            self.landing.setDate(QDate(y, m, d))
        except Exception:
            self.landing.setDate(QDate.currentDate())
        mf.addRow("Callsign (HUD subtitle)", self.callsign)
        mf.addRow("Landing date (SOL 0)", self.landing)
        note = QLabel("SOL is days since the landing date. Changing it affects NEW entries "
                      "only - existing filenames keep their original SOL.")
        note.setWordWrap(True)
        mf.addRow(note)

        # ---- AI tab ------------------------------------------------------
        ai = QWidget()
        af = QFormLayout(ai)
        self.live_model = QComboBox()
        for m in LIVE_MODELS:
            self.live_model.addItem(f"{m['label']}   [{m['id']}]", m["id"])
        self._select(self.live_model, str(cfg.get("live_model", "")))
        self.summary_model = QComboBox()
        self.summary_model.setEditable(True)
        for m in SUMMARY_CHAIN:
            self.summary_model.addItem(m)
        self._select(self.summary_model, str(cfg.get("summary_model", "")))
        self.tr_mode = QComboBox()
        self.tr_mode.addItems(["SMART", "VERBATIM"])
        self._select(self.tr_mode, str(cfg.get("transcript_mode", "SMART")))
        self.tr_lang = QLineEdit(str(cfg.get("translate_target", "en-US")))
        self.vocab = QLineEdit(", ".join(cfg.get("custom_vocabulary", []) or []))
        self.api_key = QLineEdit(get_api_key())
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.test_btn = self._mk_button("Test model & key")
        self.test_btn.clicked.connect(self._run_test)
        self.test_lbl = QLabel("")
        self.test_lbl.setWordWrap(True)
        af.addRow("Live model (transcript engine)", self.live_model)
        af.addRow("Summary model (standard call)", self.summary_model)
        af.addRow("Transcript mode (transcribe-live)", self.tr_mode)
        af.addRow("Translate target (Live Translate)", self.tr_lang)
        af.addRow("Custom vocabulary (ASR bias)", self.vocab)
        af.addRow("GEMINI_API_KEY (.env)", self.api_key)
        h = QHBoxLayout()
        h.addWidget(self.test_btn)
        h.addWidget(self.test_lbl, 1)
        af.addRow(h)

        # ---- Capture tab -------------------------------------------------
        cap = QWidget()
        cf = QFormLayout(cap)
        self.resolution = QComboBox()
        for w, hgt in RESOLUTIONS:
            self.resolution.addItem(f"{w} x {hgt}", (w, hgt))
        self._select(self.resolution, tuple(cfg.get("resolution", [1280, 720])))
        self.fps = QComboBox()
        for v in (24, 25, 30, 60):
            self.fps.addItem(str(v), v)
        self._select(self.fps, int(cfg.get("fps", 30)))
        self.camera = QSpinBox()
        self.camera.setRange(0, 8)
        self.camera.setValue(int(cfg.get("camera_index", 0)))
        self.audio_dev = QComboBox()
        self.audio_dev.addItem("System default", "")
        if sd is not None:
            try:
                for idx, dev in enumerate(sd.query_devices()):
                    if dev.get("max_input_channels", 0) > 0:
                        self.audio_dev.addItem(f"[{idx}] {dev['name']}", dev["name"])
            except Exception:
                pass
        self._select(self.audio_dev, str(cfg.get("audio_device", "")))
        cf.addRow("Resolution", self.resolution)
        cf.addRow("FPS", self.fps)
        cf.addRow("Camera index", self.camera)
        cf.addRow("Microphone (input)", self.audio_dev)

        # ---- HUD tab -----------------------------------------------------
        hud = QWidget()
        hf = QFormLayout(hud)
        self.palette = QComboBox()
        for p in PALETTES:
            self.palette.addItem(p, p)
        self._select(self.palette, str(cfg.get("palette", "mission_white")))
        self.font_c = QComboBox()
        for f in FONTS:
            self.font_c.addItem(f, f)
        self._select(self.font_c, str(cfg.get("font", "ShareTechMono")))
        self.hud_checks: dict[str, QCheckBox] = {}
        self.hud_op = self._slider(float(cfg.get("hud.opacity", 1.0)), 0.4, 2.0)
        self.hud_backing = QCheckBox("Dark backing plate behind HUD bands (bright scenes)")
        self.hud_backing.setChecked(bool(cfg.get("hud.backing", False)))
        hf.addRow("HUD strength (0.4 - 2.0)", self.hud_op)
        hf.addRow(self.hud_backing)
        for key, label in (("sol_date", "SOL + Earth date/time (top-left)"),
                           ("title", "VIDEO LOG title + callsign (top-center)"),
                           ("rec", "REC dot + timecode (top-right)"),
                           ("stats", "Laptop telemetry + mic bar (bottom-left)"),
                           ("res_info", "Resolution/FPS/disk (bottom-right)"),
                           ("brackets", "Corner brackets + crosshair"),
                           ("live_transcript", "Live transcript ticker")):
            cb = QCheckBox(label)
            cb.setChecked(bool(cfg.get(f"hud.{key}", True)))
            self.hud_checks[key] = cb
            hf.addRow(cb)

        # ---- Effects tab -------------------------------------------------
        eff = QWidget()
        ef = QFormLayout(eff)
        self.scan_style = QComboBox()
        self.scan_style.addItem("fixed", "fixed")
        self.scan_style.addItem("rolling", "rolling")
        self.scan_style.addItem("off", "off")
        self._select(self.scan_style, str(cfg.get("effects.scanline_style", "fixed")))
        self.scan_op = self._slider(float(cfg.get("effects.scanline_opacity", 0.12)), 0.0, 0.5)
        self.vign = self._slider(float(cfg.get("effects.vignette", 0.15)), 0.0, 0.5)
        self.noise = self._slider(float(cfg.get("effects.noise", 0.02)), 0.0, 0.10)
        self.chromatic = QCheckBox("Chromatic aberration on HUD text")
        self.chromatic.setChecked(bool(cfg.get("effects.chromatic", False)))
        self.boot = QCheckBox("Boot sequence on REC (0.8 s)")
        self.boot.setChecked(bool(cfg.get("effects.boot_sequence", True)))
        ef.addRow("Scanline style", self.scan_style)
        ef.addRow("Scanline opacity", self.scan_op)
        ef.addRow("Vignette", self.vign)
        ef.addRow("Film grain / noise", self.noise)
        ef.addRow(self.chromatic)
        ef.addRow(self.boot)

        tabs.addTab(mission, "Mission")
        tabs.addTab(ai, "AI Models")
        tabs.addTab(cap, "Capture")
        tabs.addTab(hud, "HUD")
        tabs.addTab(eff, "Effects")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

    # -- helpers -----------------------------------------------------------
    def _mk_button(self, text: str):
        from PySide6.QtWidgets import QPushButton
        return QPushButton(text)

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)
        elif combo.isEditable():
            combo.setEditText(str(value))

    @staticmethod
    def _slider(value: float, lo: float, hi: float) -> QSlider:
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(int(lo * 100), int(hi * 100))
        s.setValue(int(value * 100))
        return s

    @staticmethod
    def _slider_value(s: QSlider) -> float:
        return s.value() / 100.0

    def _run_test(self) -> None:
        if self._test_thread is not None and self._test_thread.isRunning():
            return
        if self.api_key.text().strip():
            set_env("GEMINI_API_KEY", self.api_key.text().strip())
        self.test_lbl.setText("testing...")
        self._test_thread = _ModelTest(self.live_model.currentData())
        self._test_thread.done.connect(self._test_done)
        self._test_thread.start()

    def _test_done(self, ok: bool, msg: str) -> None:
        self.test_lbl.setText(("OK: " if ok else "PROBLEM: ") + msg)

    def _save(self) -> None:
        c = self.cfg
        c.set("callsign", self.callsign.text().strip() or "MARK WATNEY")
        qd = self.landing.date()
        c.set("landing_date", f"{qd.year():04d}-{qd.month():02d}-{qd.day():02d}")
        c.set("live_model", self.live_model.currentData())
        sm = self.summary_model.currentText().strip()
        c.set("summary_model", sm or "gemini-3.8-flash")
        c.set("transcript_mode", self.tr_mode.currentText())
        c.set("translate_target", self.tr_lang.text().strip() or "en-US")
        vocab = [v.strip() for v in self.vocab.text().split(",") if v.strip()]
        c.set("custom_vocabulary", vocab[:100])
        if self.api_key.text().strip():
            set_env("GEMINI_API_KEY", self.api_key.text().strip())
        res = self.resolution.currentData()
        c.set("resolution", [int(res[0]), int(res[1])])
        c.set("fps", int(self.fps.currentData()))
        c.set("camera_index", int(self.camera.value()))
        c.set("audio_device", self.audio_dev.currentData() or "")
        c.set("palette", self.palette.currentData())
        c.set("font", self.font_c.currentData())
        for key, cb in self.hud_checks.items():
            c.set(f"hud.{key}", cb.isChecked())
        c.set("hud.opacity", self._slider_value(self.hud_op))
        c.set("hud.backing", self.hud_backing.isChecked())
        c.set("effects.scanline_style", self.scan_style.currentData())
        c.set("effects.scanline_opacity", self._slider_value(self.scan_op))
        c.set("effects.vignette", self._slider_value(self.vign))
        c.set("effects.noise", self._slider_value(self.noise))
        c.set("effects.chromatic", self.chromatic.isChecked())
        c.set("effects.boot_sequence", self.boot.isChecked())
        c.save()
        self.accept()
