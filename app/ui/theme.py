"""Ares-mission dark theme + bundled font registration."""
from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase

from app.hud.fonts import font_file

DARK_QSS = """
QMainWindow, QDialog { background: #0b0e12; }
QMainWindow::separator { width: 0; }

QWidget { color: #d8d5c8; font-size: 13px; }
QLabel { background: transparent; }
QLabel[hud="true"] { color: #9fb2a4; font-size: 12px; }

QPushButton {
    background: #161b22; border: 1px solid #2a313c;
    border-radius: 4px; padding: 7px 16px; color: #d8d5c8;
}
QPushButton:hover { border-color: #4a5568; background: #1b2230; }
QPushButton:pressed { background: #232b3a; }
QPushButton:disabled { color: #566; border-color: #222; }
QPushButton#recBtn {
    background: #8e1d16; border: 1px solid #c23b30;
    font-weight: bold; padding: 10px 34px; font-size: 15px;
}
QPushButton#recBtn:hover { background: #a8231b; }
QPushButton#recBtn[recording="true"] { background: #b3261e; }
QPushButton#pauseBtn { min-width: 110px; }

QComboBox, QLineEdit, QDateEdit, QSpinBox {
    background: #12161d; border: 1px solid #2a313c; border-radius: 4px; padding: 4px 8px;
}
QComboBox QAbstractItemView { background: #12161d; selection-background-color: #24344d; }
QTableView {
    background: #0e1219; alternate-background-color: #10151d;
    gridline-color: #1d2430; border: 1px solid #1d2430;
}
QHeaderView::section {
    background: #12161d; border: 0; border-right: 1px solid #1d2430;
    padding: 6px; color: #9fb2a4;
}
QTableView::item:selected { background: #24344d; color: #fff; }
QTextEdit { background: #0e1219; border: 1px solid #1d2430; }
QSlider::groove:horizontal { height: 4px; background: #1d2430; border-radius: 2px; }
QSlider::handle:horizontal { background: #c23b30; width: 12px; margin: -5px 0; border-radius: 6px; }
QTabWidget::pane { border: 1px solid #1d2430; }
QTabBar::tab { padding: 8px 18px; background: #12161d; }
QTabBar::tab:selected { background: #1b2230; color: #fff; }
QStatusBar { background: #0e1219; color: #8ea0a6; }
QMessageBox { background: #12161d; }
QProgressBar { background: #12161d; border: 1px solid #2a313c; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px; }
"""


def apply(app, family_hint: str | None) -> None:
    app.setStyleSheet(DARK_QSS)
    if family_hint:
        f = QFont(family_hint)
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        app.setFont(f)


def register_fonts() -> str | None:
    """Register bundled TTFs with Qt; returns a usable family name or None."""
    family = None
    for name in ("ShareTechMono", "Michroma", "Orbitron"):
        path = font_file(name)
        if path and path.exists() and path.parent.name == "fonts":
            fid = QFontDatabase.addApplicationFont(str(path))
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams and family is None:
                family = fams[0]
    return family
