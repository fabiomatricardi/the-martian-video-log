"""Gemini Live API model registry (verified via ai.google.dev, Oct 2026).

Quotas observed in AI Studio (RPM / TPM / RPD): Live models are unlimited on
RPM and RPD; only TPM caps apply (1M / 65K / 20K) - far above what a single
streaming session consumes (~2K tokens per minute of speech).
"""
from __future__ import annotations

LIVE_MODELS: list[dict] = [
    # Registry mirrors the account-visible models.list (verified Oct 2026).
    # Dialog models stream with AUDIO output modality + input transcription;
    # they reject TEXT modality outright.
    {"label": "Gemini 3.8 Live  (Recommended)", "id": "gemini-3.8-live", "kind": "dialog", "tpm": "65K"},
    {"label": "Gemini 3.1 Flash Live  (dialog)", "id": "gemini-3.1-flash-live-preview", "kind": "dialog", "tpm": "65K"},
    {"label": "Gemini 3.8 Live Extended Thinking  (experimental)", "id": "gemini-3.8-live-extended-thinking", "kind": "dialog", "tpm": "65K"},
    {"label": "Gemini 3.5 Transcribe Live  (pure ASR)", "id": "gemini-3.5-transcribe-live", "kind": "stt", "tpm": "20K"},
    {"label": "Gemini 3.5 Live Translate  (speech -> English)", "id": "gemini-3.5-live-translate-preview", "kind": "translate", "tpm": "20K"},
    {"label": "Gemini 2.5 Flash Native Audio  (legacy)", "id": "gemini-2.5-flash-native-audio-latest", "kind": "dialog", "tpm": "1M"},
]

DEFAULT_LIVE_MODEL = "gemini-3.8-live"
# Evidence-based fallback ladder (Oct 2026):
#   - gemini-2.5-flash / 2.5-flash-lite are 404 for new accounts (removed)
#   - during Flash-tier 503 storms the Lite tier keeps responding, so
#     3.5-flash-lite sits before the aliases; the *-latest aliases always
#     track the current models as final backstops.
SUMMARY_CHAIN = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite",
                 "gemini-flash-latest", "gemini-flash-lite-latest"]


def model_kind(model_id: str) -> str:
    for m in LIVE_MODELS:
        if m["id"] == model_id:
            return m["kind"]
    return "dialog"


def model_label(model_id: str) -> str:
    for m in LIVE_MODELS:
        if m["id"] == model_id:
            return m["label"]
    return model_id
