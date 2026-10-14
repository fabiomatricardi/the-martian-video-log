"""Library screen: entry table + detail pane (player, summary, transcript)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSlider, QSplitter,
                               QTableWidget, QTableWidgetItem, QTextEdit,
                               QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from app.ai.jobs import JobRunner
from app.core.config import LOGS_DIR
from app.core.export import export_csv, export_zip
from app.db.repo import STATUS_DONE, STATUS_ERROR, STATUS_PROCESSING, Repo

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    _MM_OK = True
except Exception:  # pragma: no cover - QtMultimedia missing
    _MM_OK = False

STATUS_LABEL = {
    STATUS_DONE: "DONE",
    STATUS_PROCESSING: "SUMMARIZING...",
    STATUS_ERROR: "ERROR (retryable)",
    "recording": "RECORDING",
}


class LibraryView(QWidget):
    def __init__(self, cfg, repo: Repo, jobs: JobRunner, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.repo = repo
        self.jobs = jobs
        self._rows: list[dict] = []
        self._current_id: int | None = None
        self._build_ui()
        self._wire()
        if _MM_OK:
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(self._on_duration)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Log", "SOL", "Started", "Dur", "Status", "Title"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 70)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 150)
        self.table.verticalHeader().setVisible(False)

        self.title_lbl = QLabel("")
        self.title_lbl.setStyleSheet("font-size:16px; font-weight:bold; color:#e8e4d8;")
        self.meta_lbl = QLabel("")
        self.meta_lbl.setProperty("hud", True)
        self.meta_lbl.setWordWrap(True)

        self.video: QWidget | None = None
        self.btn_play: QPushButton | None = None
        self.slider: QSlider | None = None
        if _MM_OK:
            self.player = QMediaPlayer()
            self.audio_out = QAudioOutput()
            self.player.setAudioOutput(self.audio_out)
            self.video = QVideoWidget()
            self.player.setVideoOutput(self.video)
            self.video.setMinimumHeight(240)
            self.btn_play = QPushButton("PLAY")
            self.slider = QSlider(Qt.Orientation.Horizontal)
            self.slider.setRange(0, 0)
        else:
            self.player = None
            self.note = QLabel("QtMultimedia unavailable - use 'Open in player'.")

        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)

        self.btn_retry = QPushButton("RETRY AI")
        self.btn_retrans = QPushButton("RE-TRANSCRIBE")
        self.btn_zip = QPushButton("EXPORT ZIP")
        self.btn_csv = QPushButton("EXPORT CSV")
        self.btn_folder = QPushButton("OPEN LOGS")
        self.btn_player = QPushButton("OPEN IN PLAYER")
        btns = QHBoxLayout()
        for b in (self.btn_retry, self.btn_retrans, self.btn_zip, self.btn_csv,
                  self.btn_player, self.btn_folder):
            btns.addWidget(b)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(8, 8, 8, 8)
        dl.addWidget(self.title_lbl)
        dl.addWidget(self.meta_lbl)
        if _MM_OK:
            pv = QHBoxLayout()
            pv.addWidget(self.slider, 1)
            pv.addWidget(self.btn_play)
            dl.addWidget(self.video, 1)
            dl.addLayout(pv)
        else:
            dl.addWidget(self.note)
        dl.addWidget(self.text_view, 1)
        dl.addLayout(btns)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.table)
        split.addWidget(detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 5)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(split)

    def _wire(self) -> None:
        self.table.itemSelectionChanged.connect(self._on_row)
        self.btn_retry.clicked.connect(lambda: self._retry(force=False))
        self.btn_retrans.clicked.connect(lambda: self._retry(force=True))
        self.btn_zip.clicked.connect(lambda: self._export(True))
        self.btn_csv.clicked.connect(lambda: self._export(False))
        self.btn_folder.clicked.connect(self._open_folder)
        self.btn_player.clicked.connect(self._open_in_player)
        self.jobs.entry_updated.connect(self._on_job_updated)
        if _MM_OK:
            self.btn_play.clicked.connect(self._toggle_play)
            self.slider.sliderMoved.connect(self.player.setPosition)

    # -- data ----------------------------------------------------------------
    def refresh(self) -> None:
        self._rows = self.repo.list_all()
        self.table.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            vals = [
                f"#{int(r['log_number'] or 0):03d}",
                f"{int(r['sol'] or 0):04d}",
                (r["started_at"] or "").replace("T", " "),
                f"{float(r['duration_s'] or 0):.0f}s",
                STATUS_LABEL.get(r["status"], r["status"] or "?"),
                (r["title"] or (r["filename"] or "")),
            ]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c == 4 and r["status"] == STATUS_ERROR:
                    item.setForeground(Qt.GlobalColor.red)
                elif c == 4 and r["status"] == STATUS_DONE:
                    item.setForeground(Qt.GlobalColor.darkGreen)
                item.setData(Qt.ItemDataRole.UserRole, int(r["id"]))
                self.table.setItem(i, c, item)
        if not self._rows:
            self._clear_detail()

    def _selected_id(self) -> int | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return int(items[0].data(Qt.ItemDataRole.UserRole))

    def _row_for(self, entry_id: int) -> int | None:
        for i, r in enumerate(self._rows):
            if int(r["id"]) == entry_id:
                return i
        return None

    def _on_job_updated(self, entry_id: int, status: str) -> None:
        i = self._row_for(entry_id)
        if i is not None:
            self._rows[i]["status"] = status
            if status == STATUS_DONE:
                fresh = self.repo.get(entry_id)
                if fresh:
                    self._rows[i] = fresh
            self.table.item(i, 4).setText(STATUS_LABEL.get(status, status))
            if entry_id == self._current_id:
                self._load_detail(entry_id)

    # -- detail ----------------------------------------------------------------
    def _on_row(self) -> None:
        entry_id = self._selected_id()
        if entry_id is None:
            return
        self._current_id = entry_id
        self._load_detail(entry_id)

    def _clear_detail(self) -> None:
        self.title_lbl.setText("")
        self.meta_lbl.setText("")
        self.text_view.setPlainText("")
        if _MM_OK:
            self.player.setSource(QUrl())

    def _load_detail(self, entry_id: int) -> None:
        row = self.repo.get(entry_id)
        if not row:
            return
        self.title_lbl.setText(row["title"] or row["filename"] or "")
        topics = []
        try:
            topics = json.loads(row["topics"] or "[]")
        except Exception:
            pass
        meta = (f"#{int(row['log_number'] or 0):03d}  |  SOL {int(row['sol'] or 0):04d}  |  "
                f"{(row['started_at'] or '').replace('T', ' ')}  |  "
                f"{float(row['duration_s'] or 0):.0f}s  |  {row['status']}"
                + (f"  |  {row['live_model']} + {row['summary_model']}"
                   if row.get("summary_model") else ""))
        if topics:
            meta += "\nTOPICS: " + " | ".join(topics)
        if row["status"] == STATUS_ERROR and row["error_msg"]:
            meta += f"\nERROR: {row['error_msg']}"
        self.meta_lbl.setText(meta)

        if _MM_OK:
            vf = LOGS_DIR / (row["video_file"] or "")
            if row["video_file"] and vf.exists():
                self.player.setSource(QUrl.fromLocalFile(str(vf)))
                self.slider.setRange(0, 0)
            else:
                self.player.setSource(QUrl())

        txt_path = LOGS_DIR / (row["text_file"] or "")
        if row["text_file"] and txt_path.exists():
            self.text_view.setPlainText(txt_path.read_text(encoding="utf-8", errors="replace"))
        else:
            parts = []
            if row["summary"]:
                parts.append(f"TITLE: {row['title']}\n\nSUMMARY:\n{row['summary']}")
            if row["transcript"]:
                parts.append(f"TRANSCRIPT:\n{row['transcript']}")
            self.text_view.setPlainText("\n\n".join(parts)
                                        or "(AI processing pending or failed - try RETRY AI)")

    # -- actions ----------------------------------------------------------------
    def _retry(self, force: bool) -> None:
        entry_id = self._selected_id()
        if entry_id is None:
            QMessageBox.information(self, "Retry AI", "Select an entry first.")
            return
        self.jobs.enqueue(entry_id, retranscribe=force)
        i = self._row_for(entry_id)
        if i is not None:
            self.table.item(i, 4).setText(STATUS_LABEL[STATUS_PROCESSING])

    def _export(self, as_zip: bool) -> None:
        default = "vlog_export.zip" if as_zip else "vlog_index.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export entries", default,
                                              "ZIP archive (*.zip)" if as_zip else "CSV (*.csv)")
        if not path:
            return
        try:
            dest = Path(path)
            if as_zip:
                export_zip(self.repo, dest)
            else:
                export_csv(self.repo, dest)
            QMessageBox.information(self, "Export", f"Exported to {dest}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def _open_folder(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(LOGS_DIR))  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOGS_DIR)))

    def _open_in_player(self) -> None:
        entry_id = self._selected_id()
        if entry_id is None:
            return
        row = self.repo.get(entry_id)
        vf = LOGS_DIR / (row["video_file"] or "") if row else None
        if vf and vf.exists():
            if sys.platform == "win32":
                os.startfile(str(vf))  # noqa: S606
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(vf)))

    # -- player helpers -----------------------------------------------------------
    def _toggle_play(self) -> None:
        if not _MM_OK:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play.setText("PLAY")
        else:
            self.audio_out.setVolume(1.0)
            self.player.play()
            self.btn_play.setText("PAUSE")

    def _on_position(self, pos: int) -> None:
        if self.slider is not None and not self.slider.isSliderDown():
            self.slider.setValue(pos)

    def _on_duration(self, dur: int) -> None:
        if self.slider is not None:
            self.slider.setRange(0, max(0, dur))
