"""Offscreen UI smoke test: full window construct, view switching, settings dialog.

Run:  QT_QPA_PLATFORM=offscreen .venv/Scripts/python tests/ui_smoke.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def main() -> int:
    app = QApplication(sys.argv)
    from app.core.config import Config
    from app.ui.main_window import MainWindow
    from app.ui.settings_dialog import SettingsDialog
    from app.ui.theme import apply as apply_theme
    from app.ui.theme import register_fonts

    family = register_fonts()
    apply_theme(app, family)

    cfg = Config.load()
    cfg.set("landing_date", "2026-01-01")
    cfg.set("callsign", "SMOKE TEST CALLSIGN")

    win = MainWindow(cfg)
    win.show()

    # exercise view switching
    win._switch(1)   # library
    win._switch(0)   # record

    # construct settings dialog (do not exec)
    dlg = SettingsDialog(cfg, win)

    # a few hundred ms of event loop so workers emit previews
    result = {}

    def finish():
        result["closed"] = True
        win.close()
        app.quit()

    QTimer.singleShot(2000, finish)
    app.exec()

    assert result.get("closed"), "window never closed"
    # recording state must be clean after close
    assert not win.record_view.is_recording()
    print("UI SMOKE OK - window, workers, library, settings dialog all constructed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
