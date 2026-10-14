"""Microphone worker: WAV capture for the MP4 + live PCM feed for the Gemini
Live session. Pauses are simply skipped (video follows the same rule), so
A/V stays in sync and the live transcript timeline remains consistent.
"""
from __future__ import annotations

import queue
import threading
import time
import wave

import numpy as np
from PySide6.QtCore import QThread, Signal


class _Decimator:
    """48 kHz -> 16 kHz box-filter decimation with sample carry."""

    def __init__(self, factor: int = 3):
        self.factor = factor
        self._carry = np.zeros(0, dtype=np.int16)

    def feed(self, block: np.ndarray) -> np.ndarray:
        data = np.concatenate([self._carry, block])
        n = (len(data) // self.factor) * self.factor
        self._carry = data[n:]
        if n == 0:
            return np.zeros(0, dtype=np.int16)
        return data[:n].reshape(-1, self.factor).mean(axis=1).astype(np.int16)


class AudioWorker(QThread):
    error = Signal(str)

    def __init__(self, cfg_dict: dict, ctx, live_feed=None, parent=None):
        super().__init__(parent)
        self._cfg = cfg_dict
        self.ctx = ctx
        self._live_feed = live_feed  # callable(chunk16_bytes)
        rate = int(self._cfg.get("audio_rate", 48000))
        factor = max(1, round(rate / 16000))
        self._decim = _Decimator(factor)
        self._live_rate = rate // factor
        self._q: queue.Queue = queue.Queue(maxsize=128)
        self._stop = False
        self._recording = False
        self._paused = False
        self._wav: wave.Wave_write | None = None
        self._wav_path: str | None = None
        self._samples = 0
        self._closed = threading.Event()
        self._level = 0.0

    def live_output_rate(self) -> int:
        """Sample rate of the chunks produced for the Live session (e.g. 16000)."""
        return self._live_rate

    # -- control ----------------------------------------------------------
    def set_live_feed(self, fn) -> None:
        """fn(chunk16_bytes) — wired to the active LiveTranscriber."""
        self._live_feed = fn

    def apply_config(self, cfg_dict: dict) -> None:
        self._cfg = cfg_dict
        rate = int(cfg_dict.get("audio_rate", 48000))
        factor = max(1, round(rate / 16000))
        self._decim = _Decimator(factor)
        self._live_rate = rate // factor

    def start_recording(self, wav_path: str) -> None:
        self._wav_path = wav_path
        self._samples = 0
        self._paused = False
        self._recording = True
        self._closed.clear()

    def pause(self, paused: bool) -> None:
        self._paused = bool(paused)

    def stop_recording(self, timeout: float = 2.0) -> dict | None:
        if not self._recording:
            return None
        self._recording = False
        self._closed.wait(timeout)
        rate = int(self._cfg.get("audio_rate", 48000))
        return {"wav_path": self._wav_path, "samples": self._samples,
                "duration_s": self._samples / max(1, rate)}

    def shutdown(self) -> None:
        self._stop = True
        self.wait(3000)

    # -- internals --------------------------------------------------------
    def _callback(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        try:
            self._q.put_nowait(indata.copy())
        except queue.Full:
            pass  # drop a block rather than corrupt the pipeline

    def run(self) -> None:
        try:
            import sounddevice as sd
        except Exception as e:  # pragma: no cover
            self.error.emit(f"sounddevice unavailable: {e}")
            return

        stream = None
        current_key = None
        while not self._stop:
            rate = int(self._cfg.get("audio_rate", 48000))
            want = str(self._cfg.get("audio_device") or "")
            key = (rate, want)
            if key != current_key or stream is None:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    stream = None
                try:
                    stream = sd.InputStream(
                        samplerate=rate, channels=1, dtype="int16",
                        blocksize=int(rate * 0.02),
                        device=(want or None), callback=self._callback,
                    )
                    stream.start()
                    current_key = key
                    self.error.emit("")  # clear previous mic errors
                except Exception as e:
                    self.error.emit(f"Could not open microphone: {e}")
                    stream = None
                    time.sleep(1.0)
                    continue

            try:
                block = self._q.get(timeout=0.2)
            except queue.Empty:
                if not self._recording and self._wav is not None:
                    self._close_wav()
                continue
            data = np.frombuffer(block, dtype=np.int16)

            # mic level (always, so the HUD bar also works in standby)
            if data.size:
                rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                lvl = max(0.0, min(1.0, rms / 12000.0))
                self._level = 0.55 * self._level + 0.45 * lvl
                self.ctx.mic_level = self._level

            if self._recording and not self._paused:
                if self._wav is None:
                    self._open_wav()
                if self._wav is not None:
                    self._wav.writeframes(data.tobytes())
                    self._samples += data.size
                if self._live_feed is not None:
                    chunk = self._decim.feed(data)
                    if chunk.size:
                        self._live_feed(chunk.tobytes())

            if not self._recording and self._wav is not None:
                self._close_wav()

        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if self._wav is not None:
            self._close_wav()

    def _open_wav(self) -> None:
        try:
            rate = int(self._cfg.get("audio_rate", 48000))
            w = wave.open(self._wav_path, "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            self._wav = w
        except Exception as e:
            self.error.emit(f"Could not write audio file: {e}")
            self._wav = None

    def _close_wav(self) -> None:
        try:
            self._wav.close()
        except Exception:
            pass
        self._wav = None
        self._closed.set()
