"""Drawing each output frame: the camera's view of the source, on a background, with rounded corners and
a soft shadow. Everything that does not change between frames (background, shadow, corner mask) is
drawn once; per frame there is one resampling (Pillow's resize of a fractional box, so slow zooms do
not shake from rounding) and one paste.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .camera import Camera
from .media import Encoder, MediaError, VideoInfo, frames

BACKGROUNDS = {  # name -> top-left and bottom-right colours of a diagonal gradient
    "midnight": ("#0f172a", "#4338ca"),
    "ocean": ("#0e7490", "#1e3a8a"),
    "sunset": ("#f97316", "#be185d"),
    "forest": ("#14532d", "#0f766e"),
    "graphite": ("#27272a", "#52525b"),
    "paper": ("#f5f5f4", "#d6d3d1"),
}


@dataclass
class Style:
    background: str = "midnight"  # a name above, or a colour "#rrggbb"
    padding: float = 0.06  # share of the output's width around the video
    radius: float = 0.012  # corner radius, share of the output's width
    shadow: bool = True
    frame: bool = True  # False: the zoomed video alone, edge to edge


def _color(s: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", s.strip())
    if not m:
        raise ValueError(f"not a colour: {s!r} (use #rrggbb or one of {', '.join(BACKGROUNDS)})")
    v = m.group(1)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def background(style: Style, width: int, height: int) -> Image.Image:
    a, b = BACKGROUNDS.get(style.background, (style.background, style.background))
    ca, cb = np.array(_color(a), float), np.array(_color(b), float)
    yy, xx = np.mgrid[0:height, 0:width]
    p = ((xx / max(1, width - 1)) + (yy / max(1, height - 1))) / 2  # diagonal
    img = ca + (cb - ca) * p[..., None]
    return Image.fromarray(img.round().astype(np.uint8), "RGB")


def even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)  # H.264 in yuv420p needs even sizes


@dataclass
class Layout:
    out_w: int
    out_h: int
    inner_w: int
    inner_h: int
    left: int
    top: int


def layout(info: VideoInfo, style: Style, out_width: int | None = None) -> Layout:
    out_w = even(out_width or info.width)
    out_h = even(out_w * info.height / info.width)
    if not style.frame:
        return Layout(out_w, out_h, out_w, out_h, 0, 0)
    pad = style.padding * out_w
    inner_w = even(out_w - 2 * pad)
    inner_h = even(inner_w * info.height / info.width)
    if inner_h > out_h - 2 * pad:  # a tall video: the padding at top and bottom decides
        inner_h = even(out_h - 2 * pad)
        inner_w = even(inner_h * info.width / info.height)
    return Layout(out_w, out_h, inner_w, inner_h, (out_w - inner_w) // 2, (out_h - inner_h) // 2)


class Compositor:
    def __init__(self, info: VideoInfo, style: Style, lay: Layout):
        self.lay = lay
        self.style = style
        if not style.frame:
            self.base = None
            self.mask = None
            return
        base = background(style, lay.out_w, lay.out_h)
        r = max(0, int(round(style.radius * lay.out_w)))
        mask = Image.new("L", (lay.inner_w, lay.inner_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, lay.inner_w - 1, lay.inner_h - 1), radius=r, fill=255)
        if style.shadow:
            blur = max(2, int(lay.out_w * 0.012))
            shadow = Image.new("L", (lay.out_w, lay.out_h), 0)
            dy = max(1, int(lay.out_w * 0.004))
            ImageDraw.Draw(shadow).rounded_rectangle(
                (lay.left, lay.top + dy, lay.left + lay.inner_w, lay.top + lay.inner_h + dy), radius=r, fill=110)
            shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
            base = Image.composite(Image.new("RGB", base.size, (0, 0, 0)), base, shadow)
        self.base, self.mask = base, mask

    def frame(self, src: Image.Image, rect: tuple[float, float, float, float]) -> np.ndarray:
        lay = self.lay
        # resize with a fractional box: separable, so about 5x faster than an affine transform at 1080p, and it
        # filters properly when scaling down (the whole screen into the frame), where a transform aliases.
        view = src.resize((lay.inner_w, lay.inner_h), Image.Resampling.BICUBIC, box=rect)
        if self.base is None:
            return np.asarray(view)
        out = self.base.copy()
        out.paste(view, (lay.left, lay.top), self.mask)
        return np.asarray(out)


def render(path, out, info: VideoInfo, camera: Camera, style: Style, out_width: int | None = None, progress=None,
           crf: int = 18, preset: str = "medium") -> int:
    """Writes the polished video to `out` (MP4) and returns the number of frames written. It is written next
    to `out` under another name and renamed when complete: a failed or interrupted run leaves no half file,
    and does not replace a good one."""
    lay = layout(info, style, out_width)
    comp = Compositor(info, style, lay)
    out = Path(out)
    partial = out.with_name(f".{out.name}.partial")
    enc = Encoder(partial, lay.out_w, lay.out_h, info.fps, audio_from=path if info.has_audio else None, crf=crf, preset=preset)
    n = 0
    try:
        for i, f in enumerate(frames(path, info.width, info.height, info.fps)):
            t = i / info.fps
            enc.write(comp.frame(Image.fromarray(f, "RGB"), camera.rect(t)))
            n += 1
            if progress:
                progress(t)
        if not n:
            raise MediaError(f"{path}: no frames could be decoded")
        enc.close()
        os.replace(partial, out)
    except BaseException:
        enc.abort()
        partial.unlink(missing_ok=True)
        raise
    return n
