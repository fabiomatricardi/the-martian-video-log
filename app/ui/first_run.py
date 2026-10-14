"""First-run wizard: callsign + mission landing date."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, QDate
from PySide6.QtWidgets import (QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
                               QLabel, QLineEdit, QVBoxLayout)


class FirstRunDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Mission Setup - The Martian Video Log")
        self.setMinimumWidth(460)
        self.callsign_edit = QLineEdit("MARK WATNEY - MISSION SURVIVAL")
        self.landing_edit = QDateEdit(QDate.currentDate())
        self.landing_edit.setCalendarPopup(True)
        self.landing_edit.setDisplayFormat("yyyy-MM-dd")

        head = QLabel("ARES RESORT - VIDEO LOG SYSTEM")
        head.setStyleSheet("font-size: 16px; font-weight: bold; color: #e8e4d8;")
        sub = QLabel("Configure your mission identity. Both values can be changed later\n"
                     "in Settings (they affect new log entries only).")
        sub.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Callsign", self.callsign_edit)
        form.addRow("Landing date (SOL 0)", self.landing_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(sub)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def values(self) -> tuple[str, str]:
        qd = self.landing_edit.date()
        iso = date(qd.year(), qd.month(), qd.day()).isoformat()
        return self.callsign_edit.text().strip() or "MARK WATNEY", iso
