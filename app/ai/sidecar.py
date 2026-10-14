"""Sidecar writers: segments -> SRT captions, and the .txt entry file."""
from __future__ import annotations

import json

from app.core.naming import fmt_srt_time, fmt_timecode

GAP_S = 1.2          # silence gap that starts a new transcript segment
TAIL_S = 1.5         # assumed speech tail after the last piece of a segment
LEAD_S = 0.4         # small lead-in before a segment starts


def build_segments(pieces: list[tuple[float, str]]) -> list[dict]:
    """pieces: [(t_rel, text)] -> [{'start','end','text'}] merged by silence gap."""
    segs: list[dict] = []
    cur: dict | None = None
    for t, text in sorted(pieces, key=lambda p: p[0]):
        text = (text or "").strip()
        if not text:
            continue
        if cur is not None and t - cur["end"] <= GAP_S:
            cur["text"] = (cur["text"] + " " + text).strip()
            cur["end"] = t
        else:
            if cur is not None:
                cur["end"] += TAIL_S
            cur = {"start": max(0.0, t - LEAD_S), "end": t, "text": text}
            segs.append(cur)
    if cur is not None:
        cur["end"] += TAIL_S
    return segs


def to_captions(segments: list[dict], max_words: int = 12) -> list[dict]:
    """Split segments into SRT-sized captions with proportional timing."""
    caps: list[dict] = []
    for seg in segments:
        words = seg["text"].split()
        if not words:
            continue
        dur = max(0.8, seg["end"] - seg["start"])
        groups = [words[i:i + max_words] for i in range(0, len(words), max_words)]
        total = sum(len(g) for g in groups)
        t = seg["start"]
        for g in groups:
            span = dur * (len(g) / max(1, total))
            caps.append({"start": t, "end": t + span, "text": " ".join(g)})
            t += span
    # ensure non-overlapping, minimum display time
    for i, c in enumerate(caps):
        if i + 1 < len(caps):
            c["end"] = min(c["end"], caps[i + 1]["start"])
        if c["end"] - c["start"] < 0.8:
            c["end"] = c["start"] + 0.8
    return caps


def write_srt(path, captions: list[dict]) -> bool:
    if not captions:
        return False
    blocks = []
    for i, c in enumerate(captions, 1):
        blocks.append(
            f"{i}\n{fmt_srt_time(c['start'])} --> {fmt_srt_time(c['end'])}\n{c['text']}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return True


def write_txt(path, meta: dict, summary: dict | None, segments: list[dict]) -> None:
    bar = "=" * 60
    thin = "-" * 60
    lines: list[str] = [bar, " THE MARTIAN VIDEO LOG - MISSION ENTRY", bar, ""]
    lines.append(f" FILE         : {meta.get('filename', '')}.mp4")
    lines.append(f" LOG NUMBER   : #{int(meta.get('log_number', 0) or 0):03d}")
    lines.append(f" SOL          : {int(meta.get('sol', 0) or 0):04d}")
    lines.append(f" STARTED      : {meta.get('started_at', '')}")
    lines.append(f" DURATION     : {fmt_timecode(float(meta.get('duration_s', 0) or 0))}"
                 f"  ({float(meta.get('duration_s', 0) or 0):.1f} s)")
    lines.append(f" LIVE MODEL   : {meta.get('live_model', '')}")
    lines.append(f" SUMMARY AI   : {meta.get('summary_model', '') or '(pending)'}")
    lines.append(f" CALLSIGN     : {meta.get('callsign', '')}")
    lines.append("")
    lines.append(thin)
    lines.append(" SUMMARY")
    lines.append(thin)
    if summary:
        lines.append(f" {summary.get('title', '')}")
        lines.append("")
        for para in str(summary.get("summary", "")).split("\n"):
            lines.append(f" {para}")
        if summary.get("topics"):
            lines.append(f" TOPICS     : {' | '.join(summary['topics'])}")
        if summary.get("mood"):
            lines.append(f" MOOD       : {summary['mood']}")
        for h in summary.get("highlights", []):
            lines.append(f"  * {h}")
    else:
        lines.append(" (summary pending - use 'Retry AI' in the Library)")
    lines.append("")
    lines.append(thin)
    lines.append(" TRANSCRIPT")
    lines.append(thin)
    if segments:
        for s in segments:
            lines.append(f" [{fmt_timecode(s['start'])} - {fmt_timecode(s['end'])}] {s['text']}")
    else:
        lines.append(" (no speech captured)")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def segments_from_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [s for s in data if isinstance(s, dict) and "start" in s]
    except Exception:
        return []
