"""Font resolution: bundled TTFs first, then Windows system fonts, then PIL default."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import ImageFont

from app.core.config import FONTS_DIR

_FONT_FILES: dict[str, list[str]] = {
    "ShareTechMono": ["ShareTechMono-Regular.ttf", "ShareTechMono.ttf"],
    "Michroma": ["Michroma-Regular.ttf", "Michroma.ttf"],
    "Orbitron": ["Orbitron-Regular.ttf", "Orbitron.ttf", "Orbitron[wght].ttf"],
}
_SYSTEM_FALLBACKS = ["consola.ttf", "cour.ttf", "arial.ttf"]
_WINDOWS_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def _candidates(name: str) -> list[Path]:
    paths: list[Path] = []
    for fname in _FONT_FILES.get(name, [f"{name}.ttf"]):
        paths.append(FONTS_DIR / fname)
    if not paths or not any(p.exists() for p in paths):
        for fname in _SYSTEM_FALLBACKS:
            paths.append(_WINDOWS_FONTS / fname)
    return paths


def font_file(name: str) -> Path | None:
    for p in _candidates(name):
        if p.exists():
            return p
    return None


def load_pil_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = font_file(name)
    if path:
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()
