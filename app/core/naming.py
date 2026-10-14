"""Filename scheme and time formatting helpers."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


def make_basename(sol: int, start_dt: datetime) -> str:
    """VLOG_SOL0042_20261014-1932  (recording START time, minute precision)."""
    return "VLOG_SOL{:04d}_{}".format(max(0, int(sol)), start_dt.strftime("%Y%m%d-%H%M"))


def unique_path(directory: Path, basename: str, ext: str) -> Path:
    """Return a non-clobbering path; appends _2, _3 ... on same-minute collisions."""
    ext = ext.lstrip(".")
    candidate = directory / f"{basename}.{ext}"
    n = 1
    while candidate.exists():
        n += 1
        candidate = directory / f"{basename}_{n}.{ext}"
    return candidate


def sanitize(name: str) -> str:
    return SAFE_RE.sub("_", name)


def fmt_timecode(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(millis, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
