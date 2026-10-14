"""Application configuration: defaults, config.json persistence, .env handling."""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.json"
ENV_PATH = PROJECT_ROOT / ".env"
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
ASSETS_DIR = PROJECT_ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
TMP_DIR = PROJECT_ROOT / "tmp"

DEFAULTS: dict = {
    "callsign": "MARK WATNEY - MISSION SURVIVAL",
    "landing_date": "",            # ISO date (YYYY-MM-DD); empty -> first-run wizard
    "live_model": "gemini-3.8-live",
    "summary_model": "gemini-3.8-flash",
    "transcript_mode": "SMART",    # SMART | VERBATIM  (used by gemini-3.5-transcribe-live)
    "translate_target": "en-US",   # target language for the Live Translate model
    "custom_vocabulary": ["Sol", "Hab", "Watney", "Ares", "oxygenator", "IRP", "MDV",
                          "Hydroponics", "Areography", "Hermes", "EVA", "Regolith"],
    "palette": "mission_white",
    "font": "ShareTechMono",
    "hud": {
        "sol_date": True,
        "title": True,
        "rec": True,
        "stats": True,
        "res_info": True,
        "brackets": True,
        "live_transcript": True,
        "opacity": 1.0,            # 0.4 .. 2.0 - alpha multiplier for all HUD ink
        "backing": False,          # dark plate behind the HUD bands (bright scenes)
    },
    "effects": {
        "scanline_style": "fixed",   # fixed | rolling | off
        "scanline_opacity": 0.12,    # 0.0 .. 0.5
        "vignette": 0.15,            # 0.0 .. 0.5
        "noise": 0.02,               # 0.0 .. 0.10
        "chromatic": False,          # 1px R/B fringe on HUD text
        "boot_sequence": True,
    },
    "resolution": [1280, 720],
    "fps": 30,
    "camera_index": 0,
    "audio_device": "",              # empty => system default input
    "audio_rate": 48000,
    "save_dir": "logs",
}


def deep_update(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_env() -> dict:
    """Tiny .env parser (no external dependency)."""
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k == "GEMINI_API_KEY"})
    return env


def set_env(key: str, value: str) -> None:
    env = load_env()
    env[key] = value
    lines = [f"{k}={v}" for k, v in env.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_api_key() -> str:
    return load_env().get("GEMINI_API_KEY", "")


class Config:
    """Dict-backed configuration with deep-merged defaults."""

    def __init__(self, data: dict | None = None):
        self._data: dict = json.loads(json.dumps(DEFAULTS))  # deep copy
        if data:
            deep_update(self._data, data)
        self.ensure_dirs()

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return cls(data)
            except Exception:
                pass
        cfg = cls()
        cfg.save()
        return cfg

    def save(self) -> None:
        self.ensure_dirs()
        CONFIG_PATH.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def ensure_dirs(self) -> None:
        for d in (LOGS_DIR, DATA_DIR, TMP_DIR, FONTS_DIR):
            d.mkdir(parents=True, exist_ok=True)

    # -- access -----------------------------------------------------------
    def get(self, path: str, default=None):
        """cfg.get('effects.scanline_opacity')"""
        node: object = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path: str, value) -> None:
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self._data))


def sweep_tmp() -> int:
    """Delete v_*/a_* temp artifacts left over from a crashed session.

    Safe to run only at startup - no recording can be in progress yet.
    Returns how many files were removed (for logging).
    """
    if not TMP_DIR.exists():
        return 0
    removed = 0
    for p in TMP_DIR.iterdir():
        if p.is_file() and p.name.startswith(("v_", "a_")):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass  # locked by another process - leave it
    return removed
