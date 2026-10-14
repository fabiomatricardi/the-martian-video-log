"""Main window: toolbar + stacked Record/Library views, worker ownership, shutdown."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QMainWindow, QMessageBox, QStackedWidget, QToolBar,
                               QWidget)

from app.ai.jobs import JobRunner
from app.capture.audio_worker import AudioWorker
from app.capture.video_worker import VideoWorker
from app.db.repo import STATUS_PROCESSING, Repo
from app.hud.overlay import HudContext
from app.ui.library_view import LibraryView
from app.ui.record_view import RecordView
from app.ui.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("THE MARTIAN VIDEO LOG - Ares Resort Station")
        self.resize(1280, 860)

        self.repo = Repo()
        self.ctx = HudContext()
        self.jobs = JobRunner(self.repo, lambda: self.cfg.as_dict())
        self.video_worker = VideoWorker(self.cfg.as_dict(), self.ctx)
        self.audio_worker = AudioWorker(self.cfg.as_dict(), self.ctx, live_feed=None)

        self.record_view = RecordView(self.cfg, self.repo, self.jobs,
                                      self.video_worker, self.audio_worker, self.ctx)
        self.library_view = LibraryView(self.cfg, self.repo, self.jobs)
        self.record_view.go_library.connect(self._show_library)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.record_view)
        self.stack.addWidget(self.library_view)
        self.setCentralWidget(self.stack)

        self._build_toolbar()

        # resume pending AI jobs from a previous session
        pending = self.repo.by_status(STATUS_PROCESSING)
        if pending:
            for row in pending:
                self.jobs.enqueue(int(row["id"]))
            self.statusBar().showMessage(
                f"Resuming {len(pending)} pending AI job(s) from the previous session...", 8000)
        self.jobs.start()
        self.jobs.entry_updated.connect(self._on_job_updated)

        self.video_worker.start()
        self.audio_worker.start()
        self.record_view.run_preflight()

    def _build_toolbar(self) -> None:
        bar = QToolBar("main")
        bar.setMovable(False)
        self.addToolBar(bar)
        a_rec = QAction("RECORD", self)
        a_rec.triggered.connect(lambda: self._switch(0))
        a_lib = QAction("LIBRARY", self)
        a_lib.triggered.connect(lambda: self._switch(1))
        a_set = QAction("SETTINGS", self)
        a_set.triggered.connect(self._open_settings)
        for a in (a_rec, a_lib, a_set):
            bar.addAction(a)
        self._actions = (a_rec, a_lib, a_set)
        bar.addSeparator()
        a_off = QAction("⏻ SHUTDOWN", self)
        a_off.setShortcut(QKeySequence("Ctrl+Q"))
        a_off.triggered.connect(self._shutdown_request)
        bar.addAction(a_off)

    def _switch(self, idx: int) -> None:
        if idx == 1:
            self.library_view.refresh()
        self.stack.setCurrentIndex(idx)
        for i, a in enumerate(self._actions):
            a.setEnabled(i != idx)

    def _show_library(self) -> None:
        self._switch(1)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            self.video_worker.apply_config(self.cfg.as_dict())
            self.audio_worker.apply_config(self.cfg.as_dict())
            self.record_view.run_preflight()
            self.statusBar().showMessage("Settings applied.", 4000)

    def _shutdown_request(self) -> None:
        if self.record_view.is_recording():
            QMessageBox.information(
                self, "Recording in progress",
                "A log entry is being recorded. Stop the recording (STOP button) "
                "before shutting down.")
            return
        answer = QMessageBox.question(
            self, "Shutdown",
            "Shut down the video log terminal?\n\nUnfinished AI jobs stay marked "
            "'processing' and resume automatically on the next launch.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.close()

    def _on_job_updated(self, entry_id: int, status: str) -> None:
        if status == STATUS_PROCESSING:
            self.statusBar().showMessage(f"AI job running for entry #{entry_id}...", 3000)
        elif status == "done":
            self.statusBar().showMessage(f"Entry #{entry_id} ready.", 4000)

    # -- shutdown ------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.record_view.is_recording():
            QMessageBox.information(
                self, "Recording in progress",
                "A log entry is being recorded. Stop the recording (STOP button) "
                "before closing the app.")
            event.ignore()
            return
        self.statusBar().showMessage("Shutting down workers...", 2000)
        self.video_worker.shutdown()
        self.audio_worker.shutdown()
        # brief grace period for in-flight AI job; unfinished rows persist as
        # 'processing' and are auto-resumed on next launch
        self.jobs.close(timeout=3.0)
        event.accept()
