"""HUD compositor: burns mission-style overlays + CRT effects into every frame.

Design basis is 1280x720; everything scales by h/720 so other resolutions keep
the same layout proportions.

Performance architecture (~30 fps at 720p while recording):
  - SLOW layer: chrome (rules/brackets/crosshair) + all text that changes at
    most once per second (SOL, date, title, callsign, telemetry, res line,
    standby labels) - pre-composited into ONE RGBA image, rebuilt only when
    its content signature changes, pasted once per frame
  - FAST layer: REC dot + timecode, mic bar, transcript ticker, pause/boot
    overlays - drawn per frame (texts via small cached strips)
  - darkening (scanlines+vignette): cv2 per-pixel multiply on uint8
  - film grain: pre-generated saturated add/subtract pool
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw

from app.core.naming import fmt_timecode
from app.hud import effects
from app.hud.fonts import load_pil_font

PALETTES: dict[str, dict] = {
    "mission_white": {"fg": (228, 226, 214), "accent": (255, 59, 48), "warn": (255, 179, 0)},
    "green_phosphor": {"fg": (57, 255, 110), "accent": (255, 80, 70), "warn": (255, 179, 0)},
    "ares_blue": {"fg": (159, 232, 255), "accent": (255, 59, 48), "warn": (255, 179, 0)},
}

_PAD = 4          # text strip padding
_CACHE_MAX = 160  # FIFO eviction for the text strip cache


class HudContext:
    """Shared mutable state exchanged between UI / workers / HUD (attr writes are
    atomic under the GIL; values are small and single-assignment)."""

    def __init__(self) -> None:
        self.state = "standby"            # standby | recording | paused
        self.t_active = 0.0               # seconds of *active* recording
        self.sol = 0
        self.log_number = 0
        self.stats: dict = {}
        self.mic_level = 0.0              # 0..1
        self.transcript_lines: list[str] = []
        self.no_signal = False


def _hexagon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    return [
        (cx + r * np.cos(np.radians(a)), cy + r * np.sin(np.radians(a)))
        for a in range(-30, 330, 60)
    ]


def _stats_line(st: dict) -> str:
    parts = []
    bat = st.get("bat")
    parts.append(f"BAT {bat:03d}%" if bat is not None else "BAT --")
    if st.get("plugged") and bat is not None:
        parts[-1] += "+"
    cpu = st.get("cpu")
    parts.append(f"CPU {cpu:02d}%" if cpu is not None else "CPU --")
    ram = st.get("ram_gb")
    parts.append(f"RAM {ram:.1f}G" if ram is not None else "RAM --")
    return "  |  ".join(parts)


class HudOverlay:
    def __init__(self, w: int, h: int, cfg: dict):
        self.w, self.h = int(w), int(h)
        self.s = self.h / 720.0
        self.cfg = cfg
        self._rng = np.random.default_rng(7)
        self._roll_offset = 0
        self._noise_idx = 0
        self._cache: dict = {}
        self._cache_order: list = []
        self._slow_sig = None
        self._slow: Image.Image | None = None
        self.rebuild()

    # -- setup ------------------------------------------------------------
    def rebuild(self) -> None:
        pal = PALETTES.get(self.cfg.get("palette"), PALETTES["mission_white"])
        self.fg = pal["fg"]
        self.accent = pal["accent"]
        self.warn = pal["warn"]
        font_name = self.cfg.get("font", "ShareTechMono")
        self.f_big = load_pil_font(font_name, max(12, int(30 * self.s)))
        self.f_title = load_pil_font(font_name, max(11, int(21 * self.s)))
        self.f_small = load_pil_font(font_name, max(9, int(16 * self.s)))
        self.f_tiny = load_pil_font(font_name, max(8, int(13 * self.s)))
        self.hud: dict = self.cfg.get("hud", {})
        self.fx: dict = self.cfg.get("effects", {})
        self.fps = self.cfg.get("fps", 30)
        self._ghost = bool(self.fx.get("chromatic", False))
        # HUD strength: alpha multiplier for every HUD element (0.4 .. 2.0).
        # >1 boosts ink on bright scenes; backing plate alpha follows mildly.
        self._alpha_scale = max(0.4, min(2.0, float(self.hud.get("opacity", 1.0))))
        self._backing = bool(self.hud.get("backing", False))
        self._chrome = self._build_chrome()
        self._dark = effects.build_darken_layer(
            self.w, self.h,
            self.fx.get("scanline_style", "fixed"),
            float(self.fx.get("scanline_opacity", 0.12)),
            float(self.fx.get("vignette", 0.15)),
        )
        self._dark_active = bool(self._dark[..., 3].any())
        self._keep3 = effects.build_keep3(self._dark)
        self._noise_pool = self._build_noise_pool()
        # mic bar segments precomputed for the numpy painter
        seg_w, seg_h, gap = int(9 * self.s), int(10 * self.s), int(3 * self.s)
        bx, by = int(36 * self.s) + int(18 * self.s), self.h - int(28 * self.s)
        self._bar_rects = [(bx + i * (seg_w + gap), by,
                            bx + i * (seg_w + gap) + seg_w, by + seg_h)
                           for i in range(12)]
        # slow layer is pasted as 3 band crops (top band, center crosshair box,
        # bottom band) - verified pixel-identical, ~2x cheaper than a full paste
        ch = int(46 * self.s)   # half-extent of the crosshair box
        self._band_boxes = [
            (0, 0, self.w, int(80 * self.s)),
            (self.w // 2 - ch, self.h // 2 - ch, self.w // 2 + ch, self.h // 2 + ch),
            (0, self.h - int(150 * self.s), self.w, self.h),
        ]
        self._slow_crops: list[Image.Image] = []
        self._slow_sig = None
        self._slow = None

    def _build_noise_pool(self) -> list[tuple[np.ndarray, np.ndarray]]:
        amp = max(1, int(float(self.fx.get("noise", 0.0)) * 255))
        if amp <= 1:
            return []
        h, w = self.h, self.w
        pool = []
        for _ in range(6):
            n = self._rng.integers(-amp, amp + 1, size=(h, w, 1), dtype=np.int16)
            pool.append(effects.noise_pair(n))
        return pool

    # -- text strip cache ---------------------------------------------------
    def _paste_text(self, base: Image.Image, xy, text: str, font, fill,
                    anchor: str | None = None, ghost: bool = False) -> None:
        if not text:
            return
        key = (text, id(font), fill, bool(ghost))
        entry = self._cache.get(key)
        if entry is None:
            x0, y0, x1, y1 = font.getbbox(text)
            asc, desc = font.getmetrics()
            adv = float(font.getlength(text))
            ink_w = max(1, int(x1 - x0))
            ink_h = max(1, int(y1 - y0))
            extra = 2 if ghost else 0
            W, H = ink_w + 2 * _PAD + extra, ink_h + 2 * _PAD
            strip = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sd = ImageDraw.Draw(strip, "RGBA")
            ox, oy = _PAD - x0, _PAD - y0
            if ghost:
                sd.text((ox - 1, oy), text, font=font, fill=(255, 70, 70, 70))
                sd.text((ox + 1, oy), text, font=font, fill=(70, 130, 255, 70))
            sd.text((ox, oy), text, font=font, fill=fill)
            entry = (strip, x0, y0, asc, desc, adv)
            self._cache[key] = entry
            self._cache_order.append(key)
            if len(self._cache_order) > _CACHE_MAX:
                old = self._cache_order.pop(0)
                self._cache.pop(old, None)
        strip, x0, y0, asc, desc, adv = entry
        # anchor -> layout origin (PIL semantics for the anchors we use)
        ha, va = (anchor or "la")[0], (anchor or "la")[1]
        oxx = xy[0] - (adv / 2.0 if ha == "m" else (adv if ha == "r" else 0.0))
        oyy = xy[1] - ((asc + desc) / 2.0 if va == "m" else 0.0)
        dest = (int(round(oxx + x0 - _PAD)), int(round(oyy + y0 - _PAD)))
        base.paste(strip, dest, strip)

    def _al(self, v: int) -> int:
        """Scale a HUD alpha by the configured opacity, capped at 255."""
        return min(255, max(0, int(round(v * self._alpha_scale))))

    def _build_backing(self) -> Image.Image:
        """Dark plate under the top/bottom HUD bands (alpha mildly follows opacity)."""
        w, h, s = self.w, self.h, self.s
        plate = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(plate)
        a = int(105 * min(self._alpha_scale, 1.3))
        pd.rectangle([0, 0, w, int(80 * s)], fill=(8, 10, 12, a))
        pd.rectangle([0, h - int(150 * s), w, h], fill=(8, 10, 12, a))
        return plate

    # -- static chrome (rules, brackets, crosshair) -------------------------
    def _build_chrome(self) -> Image.Image:
        w, h, M, s = self.w, self.h, int(36 * self.s), self.s
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        col = self.fg + (self._al(120),)

        top_rule, bot_rule = int(78 * s), h - int(62 * s)
        d.line([(M, top_rule), (w - M, top_rule)], fill=col, width=max(1, int(s)))
        d.line([(M, bot_rule), (w - M, bot_rule)], fill=col, width=max(1, int(s)))
        for i in range(13):  # ruler ticks
            x = M + i * (w - 2 * M) / 12.0
            t = 7 if i % 3 == 0 else 4
            d.line([(x, top_rule - t * s), (x, top_rule)], fill=col, width=1)
            d.line([(x, bot_rule), (x, bot_rule + t * s)], fill=col, width=1)

        if self.hud.get("brackets", True):
            for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                bx = M if sx > 0 else w - M
                by = M if sy > 0 else h - M
                arm_x, arm_y = int(46 * s), int(10 * s)
                d.line([(bx, by), (bx + sx * arm_x, by)], fill=col, width=2)
                d.line([(bx, by), (bx, by + sy * arm_y)], fill=col, width=2)
                hx = bx + sx * int(8 * s)
                hy = by + sy * int(8 * s)
                d.polygon(_hexagon(hx, hy, 4 * s), outline=col, width=1)

            cx, cy, r = w // 2, h // 2, int(24 * s)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=self.fg + (self._al(130),), width=2)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                d.line([(cx + dx * r, cy + dy * r),
                        (cx + dx * (r + int(10 * s)), cy + dy * (r + int(10 * s)))],
                       fill=self.fg + (self._al(130),), width=2)
            d.ellipse([cx - 3 * s, cy - 3 * s, cx + 3 * s, cy + 3 * s],
                      fill=self.fg + (self._al(170),))
        return img

    # -- SLOW layer (rebuilt ~1x/s or on content change) ----------------------
    def _build_slow(self, ctx: HudContext, wall: datetime) -> Image.Image:
        w, h, M, s = self.w, self.h, int(36 * self.s), self.s
        inset = int(18 * s)
        fg = self.fg
        if self._backing:
            img = Image.alpha_composite(self._build_backing(), self._chrome)
        else:
            img = self._chrome.copy()
        d = ImageDraw.Draw(img, "RGBA")

        if self.hud.get("sol_date", True):
            self._paste_text(img, (M + inset, int(14 * s)),
                             "SOL {:04d}".format(max(0, ctx.sol)),
                             self.f_big, fg + (self._al(255),), ghost=self._ghost)
            self._paste_text(img, (M + inset, int(54 * s)),
                             wall.strftime("%Y-%m-%d  %H:%M:%S"),
                             self.f_small, fg + (self._al(185),))

        if self.hud.get("title", True):
            title = ("VIDEO LOG - #{:03d}".format(ctx.log_number)
                     if ctx.state != "standby" else "VIDEO LOG - STANDBY")
            self._paste_text(img, (w / 2, int(16 * s)), title, self.f_title, fg + (self._al(240),),
                             anchor="mt", ghost=self._ghost)
            cs = self.cfg.get("callsign", "")
            if cs:
                self._paste_text(img, (w / 2, int(47 * s)), cs, self.f_small, fg + (self._al(170),),
                                 anchor="mt")

        if self.hud.get("rec", True) and ctx.state == "standby":
            xr = w - M - inset
            self._paste_text(img, (xr, int(16 * s)), "STANDBY", self.f_title, fg + (self._al(160),),
                             anchor="ra")
            self._paste_text(img, (xr, int(50 * s)), wall.strftime("SYS %H:%M:%S"),
                             self.f_small, fg + (self._al(140),), anchor="ra")

        if self.hud.get("stats", True):
            self._paste_text(img, (M + inset, h - int(52 * s)),
                             _stats_line(ctx.stats or {}), self.f_small, fg + (self._al(215),))

        if self.hud.get("res_info", True):
            disk = (ctx.stats or {}).get("disk_gb")
            txt = f"{w}x{h} . {self.fps}FPS" + (f" . DISK {disk:.0f}G" if disk else "")
            self._paste_text(img, (w - M - inset, h - int(52 * s)), txt, self.f_small,
                             fg + (self._al(185),), anchor="ra")
        return img

    def _slow_signature(self, ctx: HudContext, wall: datetime) -> tuple:
        return (
            wall.strftime("%Y-%m-%d %H:%M:%S"),
            ctx.state, ctx.sol, ctx.log_number,
            _stats_line(ctx.stats or {}) if self.hud.get("stats", True) else "",
            (ctx.stats or {}).get("disk_gb") if self.hud.get("res_info", True) else None,
        )

    # -- FAST layer (every frame) ---------------------------------------------
    def _draw_fast(self, base: Image.Image, ctx: HudContext, wall: datetime) -> None:
        w, h, M, s = self.w, self.h, int(36 * self.s), self.s
        inset = int(18 * s)
        fg, accent, warn = self.fg, self.accent, self.warn
        state = ctx.state
        recording = state == "recording"
        paused = state == "paused"
        d = ImageDraw.Draw(base, "RGBA")

        # -- top-right: REC / STBY dot + timecode
        if self.hud.get("rec", True) and (recording or paused):
            xr = w - M - inset
            tc = fmt_timecode(ctx.t_active)
            label = ("REC " if recording else "STBY ") + tc
            tw = self.f_title.getlength(label)
            x_text = xr - tw
            color = accent if recording else warn
            blink = int(ctx.t_active * 2) % 2 == 0
            dot_alpha = self._al(240) if (blink or paused) else self._al(90)
            r = int(8 * s)
            cy = int(16 * s) + int(11 * s)
            d.ellipse([x_text - int(26 * s) - r, cy - r, x_text - int(26 * s) + r, cy + r],
                      fill=color + (dot_alpha,))
            self._paste_text(base, (x_text, int(16 * s)), label, self.f_title,
                             color + (self._al(245),), ghost=self._ghost)

        # -- bottom-left: mic level bar is painted in numpy (see _paint_mic_bar)

        # -- live transcript ticker (bottom-center, above bottom rule)
        if self.hud.get("live_transcript", True) and (recording or paused):
            lines = [t for t in (ctx.transcript_lines or []) if t][-2:]
            y = int(h - 118 * s)
            for line in lines:
                if not line:
                    continue
                tw = self.f_small.getlength(line)
                while tw > w - 2 * M and len(line) > 8:  # crude clip
                    line = line[:-2]
                    tw = self.f_small.getlength(line + "...")
                if tw > w - 2 * M and len(line) <= 8:
                    line = ""
                self._paste_text(base, (w / 2, y), line, self.f_small, fg + (self._al(225),),
                                 anchor="mt")
                y += int(24 * s)

        # -- paused overlay
        if paused:
            if int(wall.timestamp() * 2) % 2 == 0:
                self._paste_text(base, (w / 2, h / 2 - int(78 * s)), "PAUSED", self.f_big,
                                 warn + (self._al(235),), anchor="mm")
            self._paste_text(base, (w / 2, h / 2 - int(44 * s)), "MISSION TIME FROZEN",
                             self.f_tiny, warn + (self._al(190),), anchor="mm")

        # -- boot sequence flash
        if (self.fx.get("boot_sequence", True) and recording
                and ctx.t_active < 0.8 and ctx.log_number > 0):
            t = ctx.t_active
            box_w, box_h = int(560 * s), int(110 * s)
            bx, by = w / 2 - box_w / 2, h / 2 - int(120 * s)
            d.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=int(10 * s),
                                fill=(10, 12, 14, self._al(205)), outline=fg + (self._al(160),),
                                width=2)
            self._paste_text(base, (w / 2, by + int(30 * s)),
                             "ARES RESORT - VIDEO LOG SYSTEM v2.1",
                             self.f_small, fg + (self._al(235),), anchor="mm")
            self._paste_text(base, (w / 2, by + int(72 * s)),
                             "SOL {:04d} - RECORDING INITIALIZED".format(max(0, ctx.sol)),
                             self.f_title, fg + (self._al(245),), anchor="mm")
            flash_alpha = int(max(0.0, 1 - t / 0.8) * 70)
            if flash_alpha:
                d.rectangle([0, 0, w, h], fill=(255, 255, 255, flash_alpha))

        # -- no signal (standby with dead camera)
        if ctx.no_signal and state == "standby":
            if int(wall.timestamp() * 2) % 2 == 0:
                self._paste_text(base, (w / 2, h / 2), "NO SIGNAL", self.f_big,
                                 accent + (self._al(240),), anchor="mm")

    # -- mic bar (numpy, ~0.3 ms for all 12 segments) -------------------------
    def _paint_mic_bar(self, out: np.ndarray, ctx: HudContext) -> None:
        if not self.hud.get("stats", True):
            return
        filled = int(max(0.0, min(1.0, ctx.mic_level)) * 12)
        hot = ctx.mic_level > 0.92
        for i, (x0, y0, x1, y1) in enumerate(self._bar_rects):
            a = self._al(225) if i < filled else self._al(55)
            color = self.warn if (hot and i >= 10) else self.fg
            region = out[y0:y1, x0:x1]
            out[y0:y1, x0:x1] = ((region.astype(np.uint16) * (255 - a)
                                  + np.asarray(color, np.uint16) * a + 127)
                                 // 255).astype(np.uint8)

    # -- compose -----------------------------------------------------------
    def compose(self, frame_rgb: np.ndarray, ctx: HudContext,
                wall: datetime | None = None) -> np.ndarray:
        wall = wall or datetime.now()
        base = Image.fromarray(frame_rgb, "RGB")
        sig = self._slow_signature(ctx, wall)
        if sig != self._slow_sig or self._slow is None:
            self._slow = self._build_slow(ctx, wall)
            self._slow_sig = sig
            self._slow_crops = [self._slow.crop(b) for b in self._band_boxes]
        if self._slow is not None:
            for box, crop in zip(self._band_boxes, self._slow_crops):
                base.paste(crop, (box[0], box[1]), crop)
        self._draw_fast(base, ctx, wall)

        out = np.array(base)  # writable copy of h,w,3 uint8
        self._paint_mic_bar(out, ctx)
        if self._dark_active:
            keep3 = self._keep3
            if self.fx.get("scanline_style", "fixed") == "rolling":
                keep3 = effects.roll_layer(keep3, self._roll_offset)
                self._roll_offset = (self._roll_offset + 1) % self.h
            out = effects.blend_darken_fast(out, keep3)
        if self._noise_pool:
            pos3, neg3 = self._noise_pool[self._noise_idx]
            self._noise_idx = (self._noise_idx + 1) % len(self._noise_pool)
            out = effects.add_noise_fast(out, pos3, neg3)
        return out
