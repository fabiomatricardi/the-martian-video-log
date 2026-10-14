"""Background AI job runner: single worker thread, sequential jobs.

Job = (entry_id, retranscribe). Steps:
  1. ensure transcript (live-session transcript already stored, else stream the
     recorded MP4's audio through the selected Live model)
  2. summarize via standard Gemini call (fallback chain)
  3. write .txt + .srt sidecars
  4. update DB (processing -> done | error)

Pending 'processing' rows are auto-resumed on app start; failed rows are
retryable from the Library screen.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.ai import sidecar
from app.ai.live_session import LiveTranscriber
from app.ai.models import model_kind
from app.ai.summarizer import summarize
from app.core.config import LOGS_DIR, TMP_DIR, get_api_key
from app.capture.muxer import get_ffmpeg_exe
from app.db.repo import Repo, STATUS_DONE, STATUS_ERROR, STATUS_PROCESSING

log = logging.getLogger(__name__)

# 100 ms of 16 kHz mono 16-bit PCM = 3200 bytes
CHUNK_BYTES = 3200
FILE_FEED_PACE_S = 0.05  # ~2x realtime when transcribing from the recorded file


def extract_pcm16(mp4_path: Path) -> bytes | None:
    """Extract 16 kHz mono 16-bit PCM from the final MP4 via ffmpeg."""
    try:
        import subprocess
        exe = get_ffmpeg_exe()
        out = TMP_DIR / f"pcm_{uuid.uuid4().hex}.s16"
        cmd = [exe, "-y", "-i", str(mp4_path), "-vn", "-ar", "16000", "-ac", "1",
               "-f", "s16le", str(out)]
        subprocess.run(cmd, capture_output=True, timeout=600,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        if out.exists() and out.stat().st_size > 0:
            data = out.read_bytes()
            out.unlink(missing_ok=True)
            return data
        out.unlink(missing_ok=True)
    except Exception:
        return None
    return None


class JobRunner(QObject):
    entry_updated = Signal(int, str)   # entry_id, new status

    def __init__(self, repo: Repo, cfg_getter):
        super().__init__()
        self._repo = repo
        self._cfg_getter = cfg_getter      # -> fresh cfg dict per job
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="AiJobRunner")
        self._started = False

    # -- public ------------------------------------------------------------
    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def enqueue(self, entry_id: int, retranscribe: bool = False) -> None:
        self._q.put((int(entry_id), bool(retranscribe)))

    def close(self, timeout: float = 45.0) -> None:
        if self._started:
            self._q.put(None)
            self._thread.join(timeout)

    # -- worker ------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                break
            entry_id, retranscribe = job
            try:
                self._process(entry_id, retranscribe)
            except Exception as e:
                log.warning("job %s crashed: %s", entry_id, e)
                self._repo.update(entry_id, status=STATUS_ERROR,
                                  error_msg=f"job: {e}"[:400])
                self.entry_updated.emit(entry_id, STATUS_ERROR)

    def _process(self, entry_id: int, retranscribe: bool) -> None:
        row = self._repo.get(entry_id)
        if not row:
            return
        cfg = self._cfg_getter()
        api_key = get_api_key()
        if not api_key:
            self._repo.update(entry_id, status=STATUS_ERROR,
                              error_msg="GEMINI_API_KEY missing (check .env or Settings)")
            self.entry_updated.emit(entry_id, STATUS_ERROR)
            return

        base = row["filename"] or f"entry{entry_id}"
        log.info("job %s: start (retranscribe=%s, model=%s)",
                 entry_id, retranscribe, row["live_model"] or cfg.get("live_model"))
        self._repo.update(entry_id, status=STATUS_PROCESSING, error_msg="")
        self.entry_updated.emit(entry_id, STATUS_PROCESSING)

        # -- 1) transcript -------------------------------------------------
        transcript = (row["transcript"] or "").strip()
        segments = sidecar.segments_from_json(row["segments"])
        if (not transcript or not segments or retranscribe) and row["video_file"]:
            try:
                pieces = self._transcribe_from_file(
                    LOGS_DIR / row["video_file"],
                    row["live_model"] or cfg.get("live_model"),
                    cfg)
            except Exception as e:
                log.warning("job %s: transcribe stage failed: %s", entry_id, e)
                self._repo.update(entry_id, status=STATUS_ERROR,
                                  error_msg=f"transcribe: {e}"[:400])
                self.entry_updated.emit(entry_id, STATUS_ERROR)
                return
            segments = sidecar.build_segments(pieces)
            transcript = " ".join(s["text"] for s in segments).strip()
            self._repo.update(entry_id, transcript=transcript,
                              segments=json.dumps(segments, ensure_ascii=False))
            log.info("job %s: transcribe stage done (%d segments, %.0f chars)",
                     entry_id, len(segments), len(transcript))
            if not transcript:
                log.info("job %s: transcription returned no text "
                         "(session ok, 0 segments)", entry_id)

        # -- 2) summary ----------------------------------------------------
        meta = {
            "filename": base,
            "log_number": row["log_number"],
            "sol": row["sol"],
            "started_at": row["started_at"],
            "duration_s": row["duration_s"] or 0,
            "callsign": cfg.get("callsign", ""),
        }
        try:
            summary, used_model = summarize(
                api_key, cfg.get("summary_model", "gemini-3.8-flash"),
                meta, transcript)
        except Exception as e:
            log.warning("job %s: summary stage failed: %s", entry_id, e)
            self._repo.update(entry_id, status=STATUS_ERROR,
                              error_msg=f"summary: {e}"[:400])
            self.entry_updated.emit(entry_id, STATUS_ERROR)
            return
        log.info("job %s: summary stage done via %s", entry_id, used_model)

        # -- 3) sidecars ---------------------------------------------------
        try:
            txt_path = LOGS_DIR / f"{base}.txt"
            sidecar.write_txt(txt_path, {**meta, "live_model": row["live_model"],
                                         "summary_model": used_model},
                              summary, segments)
            srt_rel = ""
            srt_ok = sidecar.write_srt(LOGS_DIR / f"{base}.srt",
                                       sidecar.to_captions(segments))
            if srt_ok:
                srt_rel = f"{base}.srt"
        except Exception as e:
            log.warning("job %s: sidecar stage failed: %s", entry_id, e)
            self._repo.update(entry_id, status=STATUS_ERROR,
                              error_msg=f"sidecar: {e}"[:400])
            self.entry_updated.emit(entry_id, STATUS_ERROR)
            return

        size = (LOGS_DIR / row["video_file"]).stat().st_size if row["video_file"] else None
        self._repo.update(
            entry_id,
            title=summary.get("title", ""),
            summary=summary.get("summary", ""),
            topics=json.dumps(summary.get("topics", []), ensure_ascii=False),
            mood=summary.get("mood", ""),
            text_file=f"{base}.txt",
            srt_file=srt_rel,
            summary_model=used_model,
            file_size=size,
            status=STATUS_DONE,
            error_msg="",
        )
        self.entry_updated.emit(entry_id, STATUS_DONE)
        log.info("job %s: DONE (%s)", entry_id, base)

    # -- helpers -----------------------------------------------------------
    def _transcribe_from_file(self, video_path: Path, live_model: str, cfg: dict):
        if not video_path.exists():
            raise FileNotFoundError(f"missing media: {video_path}")
        pcm = extract_pcm16(video_path)
        if not pcm:
            raise RuntimeError("could not extract audio from recording (ffmpeg)")
        t0 = time.monotonic()
        tr = LiveTranscriber(get_api_key(), live_model, model_kind(live_model), cfg, t0, None)
        tr.start()
        media_len = len(pcm) / 32000.0  # seconds (16 kHz * 2 bytes)
        fed = 0
        while fed < len(pcm):
            tr.feed(pcm[fed:fed + CHUNK_BYTES])
            fed += CHUNK_BYTES
            time.sleep(FILE_FEED_PACE_S)
        # wall time of the FEED (before finish()'s drain) - finals arriving
        # during the drain must not stretch the media-time mapping
        feed_wall = max(0.001, time.monotonic() - t0)
        pieces, err = tr.finish(timeout=60)
        if err and not pieces:
            raise RuntimeError(f"transcription failed: {err}")
        # map arrival-time stamps back onto the media timeline (~2x feed)
        scale = media_len / feed_wall if feed_wall > 0 else 1.0
        scaled = [(min(t * scale, max(0.0, media_len - 0.5)), text) for t, text in pieces]
        if err:
            # partial transcript is still better than none
            pass
        return sorted(scaled, key=lambda p: p[0])
