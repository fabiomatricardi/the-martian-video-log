"""One-click export: CSV index or full ZIP archive (media + text + srt + index)."""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

from app.core.config import LOGS_DIR
from app.db.repo import Repo


def _index_csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["filename", "log_number", "sol", "started_at", "duration_s",
                     "status", "title", "summary", "live_model", "summary_model",
                     "video_file", "text_file", "srt_file", "file_size", "error_msg"])
    for r in rows:
        writer.writerow([
            r.get("filename") or "", r.get("log_number") or "", r.get("sol") or "",
            r.get("started_at") or "", r.get("duration_s") or "", r.get("status") or "",
            (r.get("title") or "").replace("\n", " "),
            (r.get("summary") or "").replace("\n", " "),
            r.get("live_model") or "", r.get("summary_model") or "",
            r.get("video_file") or "", r.get("text_file") or "",
            r.get("srt_file") or "", r.get("file_size") or "", r.get("error_msg") or "",
        ])
    return buf.getvalue().encode("utf-8-sig")


def export_csv(repo: Repo, dest: Path) -> Path:
    rows = repo.list_all()
    dest.write_bytes(_index_csv_bytes(rows))
    return dest


def export_zip(repo: Repo, dest: Path) -> Path:
    rows = repo.list_all()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.csv", _index_csv_bytes(rows))
        seen: set[str] = set()
        for r in rows:
            for key in ("video_file", "text_file", "srt_file"):
                rel = r.get(key)
                if not rel:
                    continue
                src = LOGS_DIR / rel
                if not src.exists() or rel in seen:
                    continue
                seen.add(rel)
                zf.write(src, f"logs/{rel}")
    return dest
