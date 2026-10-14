"""Structured summary generation via a standard (non-Live) Gemini call.

One reliable code path for ALL log entries: the live transcript is compressed
into a JSON summary by a cheap Flash model. Model fallback chain included.
"""
from __future__ import annotations

import json
import logging
import time

from app.ai.models import SUMMARY_CHAIN

log = logging.getLogger(__name__)

# Models learned (at runtime) to be unavailable for THIS account, e.g. the
# "no longer available to new users" 404 for gemini-2.5-flash.
_DEAD_MODELS: set[str] = set()

# Transient service-side failures worth one retry with backoff before
# falling through to the next chain member (observed: 503 "high demand",
# dropped connections under load, 429 rate limits).
_TRANSIENT = ("503", "unavailable", "high demand", "disconnected", "timeout",
              "timed out", "429", "resource_exhausted", "internal error",
              "deadline exceeded", "deadlineexceeded")
_BACKOFF_S = 3.0

SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING", "description": "Mission-log style title, max 8 words"},
        "summary": {"type": "STRING",
                    "description": "3-5 sentence mission-log summary of the entry"},
        "topics": {"type": "ARRAY", "items": {"type": "STRING"},
                   "description": "Up to 6 short lowercase topic tags"},
        "mood": {"type": "STRING", "description": "One or two words, e.g. determined"},
        "highlights": {"type": "ARRAY", "items": {"type": "STRING"},
                       "description": "Up to 5 key moments/actions, one line each"},
    },
    "required": ["title", "summary", "topics", "mood"],
}

PROMPT_TMPL = """You are the summarizer for a personal video-log system inspired by
The Martian. Each entry is one recording ("log") made by the user.

ENTRY METADATA
 filename : {filename}
 log no   : #{log_number:03d}
 sol      : {sol:04d}
 started  : {started_at}
 duration : {duration:.0f} s
 callsign : {callsign}

TRANSCRIPT (may be empty if the entry has no speech)
---
{transcript}
---

TASK
Produce the JSON object: title, summary (mission-log tone, grounded ONLY in the
transcript and metadata), topics, mood, highlights. If the transcript is empty,
base the summary on the metadata and say the log contains no speech.
"""


def _clean(data: dict) -> dict:
    out = {
        "title": str(data.get("title", "")).strip(),
        "summary": str(data.get("summary", "")).strip(),
        "topics": [str(t) for t in (data.get("topics") or [])][:8],
        "mood": str(data.get("mood", "")).strip(),
        "highlights": [str(h) for h in (data.get("highlights") or [])][:8],
    }
    return out


def summarize(api_key: str, model: str, meta: dict, transcript_text: str) -> tuple[dict, str]:
    """Returns (summary_dict, model_used). Raises on total failure."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    prompt = PROMPT_TMPL.format(
        filename=meta.get("filename", ""),
        log_number=int(meta.get("log_number", 0) or 0),
        sol=int(meta.get("sol", 0) or 0),
        started_at=meta.get("started_at", ""),
        duration=float(meta.get("duration_s", 0) or 0),
        callsign=meta.get("callsign", ""),
        transcript=(transcript_text or "(no speech detected)")[:30000],
    )
    chain = [model] + [m for m in SUMMARY_CHAIN if m != model]
    last_err: Exception | None = None
    for m in chain:
        if m in _DEAD_MODELS:
            log.info("summary chain: skipping blacklisted model %s", m)
            continue
        for attempt in (1, 2):
            try:
                resp = client.models.generate_content(
                    model=m,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SCHEMA,
                        temperature=0.4,
                    ),
                )
                raw = resp.text
                data = json.loads(raw) if raw and raw.strip().startswith("{") else (resp.parsed or {})
                if not isinstance(data, dict) or not data.get("summary"):
                    raise ValueError("empty summary payload")
                return _clean(data), m
            except Exception as e:
                last_err = e
                msg = str(e)
                log.info("summary via %s failed (attempt %d): %s",
                         m, attempt, msg[:180])
                if "404" in msg and "no longer available" in msg:
                    _DEAD_MODELS.add(m)   # gone for this account; never retry
                    break
                if attempt == 1 and any(t in msg.lower() for t in _TRANSIENT):
                    time.sleep(_BACKOFF_S)  # service-side blip: one retry
                    continue
                break
    raise last_err if last_err else RuntimeError("summary failed")


def available_models(api_key: str) -> list[str]:
    """Best-effort model listing for the Settings health check."""
    from google import genai
    client = genai.Client(api_key=api_key)
    return [m.name.replace("models/", "") for m in client.models.list()]
