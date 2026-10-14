"""Retro CRT effect layers: scanlines, vignette, noise, chromatic aberration."""
from __future__ import annotations

import numpy as np

try:
    import cv2
    _CV2 = True
except Exception:  # pragma: no cover
    _CV2 = False


def build_darken_layer(w: int, h: int, scanline_style: str, scanline_opacity: float,
                       vignette: float) -> np.ndarray:
    """RGBA (h, w, 4) uint8 layer that only darkens (color black, alpha>0).

    Combines scanlines + vignette into a single layer so compositing costs
    one multiply per frame.
    """
    alpha = np.zeros((h, w), dtype=np.float32)

    if scanline_style in ("fixed", "rolling") and scanline_opacity > 0:
        rows = (np.arange(h) % 2 == 1).astype(np.float32)
        alpha += rows[:, None] * (scanline_opacity * 255.0)

    if vignette > 0:
        yy, xx = np.mgrid[0:h, 0:w]
        cx, cy = w / 2.0, h / 2.0
        nx = (xx - cx) / (w / 2.0)
        ny = (yy - cy) / (h / 2.0)
        r = np.sqrt(nx * nx + ny * ny) / 1.4142  # 0 center .. ~1 corners
        t = np.clip((r - 0.55) / 0.45, 0.0, 1.0)
        alpha += (t * t) * (vignette * 255.0)

    alpha = np.clip(alpha, 0, 255).astype(np.uint8)
    layer = np.zeros((h, w, 4), dtype=np.uint8)
    layer[..., 3] = alpha
    return layer


def roll_layer(layer: np.ndarray, offset: int) -> np.ndarray:
    """Vertical roll for 'rolling' scanline style (cheap np.roll view)."""
    return np.roll(layer, offset, axis=0)


def build_keep3(dark_layer: np.ndarray) -> np.ndarray:
    """Per-pixel keep multipliers (h, w, 3) uint8 from a darkening layer."""
    keep = np.clip(255 - dark_layer[..., 3].astype(np.int16), 0, 255).astype(np.uint8)
    return np.dstack([keep, keep, keep])


def blend_darken(frame_rgb: np.ndarray, layer: np.ndarray) -> np.ndarray:
    """Numpy reference implementation (uint16 math)."""
    keep = 255 - layer[..., 3].astype(np.uint16)
    out = frame_rgb.astype(np.uint16)
    out *= keep[..., None]
    out >>= 8
    return out.astype(np.uint8)


def blend_darken_fast(frame_rgb: np.ndarray, keep3: np.ndarray) -> np.ndarray:
    """SIMD path: out = frame * keep3 / 256, direct uint8 (<=1 LSB error)."""
    if _CV2:
        return cv2.multiply(frame_rgb, keep3, scale=1.0 / 256.0, dtype=cv2.CV_8U)
    f16 = frame_rgb.astype(np.uint16)
    k16 = keep3.astype(np.uint16)
    return ((f16 * k16) >> 8).astype(np.uint8)


def noise_pair(noise_i16: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split signed noise into saturated add/subtract uint8 pair (3-channel)."""
    pos = np.clip(noise_i16, 0, 255).astype(np.uint8)
    neg = np.clip(-noise_i16, 0, 255).astype(np.uint8)
    return np.dstack([pos] * 3), np.dstack([neg] * 3)


def add_noise_fast(frame_rgb: np.ndarray, pos3: np.ndarray, neg3: np.ndarray) -> np.ndarray:
    if not _CV2:
        return np.clip(frame_rgb.astype(np.int16) + pos3.astype(np.int16)
                       - neg3.astype(np.int16), 0, 255).astype(np.uint8)
    return cv2.subtract(cv2.add(frame_rgb, pos3), neg3)


def apply_noise(frame_rgb: np.ndarray, strength: float, rng: np.random.Generator) -> np.ndarray:
    if strength <= 0:
        return frame_rgb
    amp = max(1, int(strength * 255))
    noise = rng.integers(-amp, amp + 1, size=(frame_rgb.shape[0], frame_rgb.shape[1], 1),
                         dtype=np.int16)
    pos3, neg3 = noise_pair(noise)
    return add_noise_fast(frame_rgb, pos3, neg3)


def apply_aberration(layer_rgba: np.ndarray, px: int = 1, blend: float = 0.6) -> np.ndarray:
    """Split red/blue channels of an RGBA HUD layer by px (subtle CRT fringe)."""
    if px <= 0:
        return layer_rgba
    r = np.roll(layer_rgba[..., 0], -px, axis=1)
    b = np.roll(layer_rgba[..., 2], px, axis=1)
    a = layer_rgba[..., 3:4].astype(np.float32) / 255.0
    out = layer_rgba.copy()
    out[..., 0] = (layer_rgba[..., 0].astype(np.float32) * (1 - blend)
                   + r.astype(np.float32) * blend).astype(np.uint8)
    out[..., 2] = (layer_rgba[..., 2].astype(np.float32) * (1 - blend)
                   + b.astype(np.float32) * blend).astype(np.uint8)
    out[..., 3:4] = (a * 255).astype(np.uint8)
    return out
