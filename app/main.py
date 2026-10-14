"""Application entry point:  python -m app.main"""
from __future__ import annotations

import logging
import sys
from datetime import date
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication

from app.core.config import LOGS_DIR, Config, sweep_tmp
from app.ui.first_run import FirstRunDialog
from app.ui.main_window import MainWindow
from app.ui.theme import apply as apply_theme
from app.ui.theme import register_fonts


def _setup_logging() -> None:
    """Rotating logs/app.log (1 MB x 3) + console, INFO level."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOGS_DIR / "app.log", maxBytes=1_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, logging.StreamHandler()])


def main() -> int:
    _setup_logging()
    logging.getLogger(__name__).info("startup: sweep_tmp removed %d tmp file(s)",
                                     sweep_tmp())
    app = QApplication(sys.argv)
    app.setApplicationName("The Martian Video Log")
    family = register_fonts()
    apply_theme(app, family)

    cfg = Config.load()
    if not cfg.get("landing_date") or not cfg.get("callsign"):
        dlg = FirstRunDialog()
        if dlg.exec():
            callsign, landing = dlg.values()
        else:  # accept defaults anyway so the wizard does not re-appear
            callsign, landing = "MARK WATNEY - MISSION SURVIVAL", date.today().isoformat()
        cfg.set("callsign", callsign)
        cfg.set("landing_date", landing)
        cfg.save()

    win = MainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
