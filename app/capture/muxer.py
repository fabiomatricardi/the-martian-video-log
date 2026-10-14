"""ffmpeg muxing: temp MP4 (mp4v, video-only) + WAV -> final MP4 (H.264 + AAC)."""
from __future__ import annotations

import subprocess


def get_ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def mux(video_tmp: str, wav_tmp: str, out_path: str, timeout: int = 900) -> tuple[bool, str]:
    """Returns (ok, message)."""
    try:
        exe = get_ffmpeg_exe()
    except Exception as e:
        return False, f"ffmpeg not available: {e}"

    cmd = [
        exe, "-y",
        "-i", video_tmp, "-i", wav_tmp,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart",
        out_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return False, f"ffmpeg failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").splitlines()[-6:]
        return False, "ffmpeg error: " + " | ".join(tail)
    return True, "ok"
