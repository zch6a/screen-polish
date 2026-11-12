"""Drawing each output frame: the camera's view of the source, on a background, with rounded corners and
a soft shadow. Everything that does not change between frames (background, shadow, corner mask) is
drawn once; per frame there is one resampling (Pillow's resize of a fractional box, so slow zooms do
not shake from rounding) and one paste.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .camera import Camera
from .media import Encoder, MediaError, VideoInfo, frames
from .timeline import Timeline

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
    aspect: float | None = None  # width / height of the output (9/16 for vertical video); None: the source's


def _color(s: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", s.strip())
    if not m:
        raise ValueError(f"not a colour: {s!r} (use #rrggbb or one of {', '.join(BACKGROUNDS)})")
    v = m.group(1)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def background(style: Style, width: int, height: int) -> Image.Image:
    a, b = BACKGROUNDS.get(style.background, (style.background, style.background))
    ca, cb = _color(a), _color(b)
    # Diagonal, from two 1-D ramps and one channel at a time in float32: full-size float64 grids took about
    # 400 MB at 4K for a moment, the peak of the whole run.
    x = np.linspace(0, 0.5, width, dtype=np.float32)[None, :]
    y = np.linspace(0, 0.5, height, dtype=np.float32)[:, None]
    p = x + y
    img = np.empty((height, width, 3), np.uint8)
    for c in range(3):
        img[..., c] = np.rint(ca[c] + (cb[c] - ca[c]) * p)
    return Image.fromarray(img, "RGB")


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
    """The output's size and where the video sits in it. With another aspect ratio than the source's, the
    output keeps the source's longer side (a 1920x1080 recording made vertical is 1080x1920)."""
    aspect = style.aspect or info.width / info.height
    if out_width:
        out_w = even(out_width)
    elif style.aspect:
        long_side = max(info.width, info.height)
        out_w = even(long_side if aspect >= 1 else long_side * aspect)
    else:
        out_w = even(info.width)
    out_h = even(out_w / aspect)
    if not style.frame:
        return Layout(out_w, out_h, out_w, out_h, 0, 0)
    pad = style.padding * out_w  # a share of the width, whatever the shape (as --help says)
    inner_w = even(out_w - 2 * pad)
    inner_h = even(inner_w / aspect)
    if inner_h > out_h - 2 * pad:  # a tall video: the padding at top and bottom decides
        inner_h = even(out_h - 2 * pad)
        inner_w = even(inner_h * aspect)
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
        x0, y0, x1, y1 = rect
        if x0 >= -0.01 and y0 >= -0.01 and x1 <= src.width + 0.01 and y1 <= src.height + 0.01:
            # resize with a fractional box: separable, so about 5x faster than an affine transform at 1080p, and it
            # filters properly when scaling down (the whole screen into the frame), where a transform aliases.
            box = (max(0.0, x0), max(0.0, y0), min(float(src.width), x1), min(float(src.height), y1))
            view = src.resize((lay.inner_w, lay.inner_h), Image.Resampling.BICUBIC, box=box)
        else:
            view = self._letterboxed(src, rect)
        if self.base is None:
            return np.asarray(view)
        out = self.base.copy()
        out.paste(view, (lay.left, lay.top), self.mask)
        return np.asarray(out)


    def _letterboxed(self, src: Image.Image, rect) -> Image.Image:
        """A view that reaches past the source (the whole of a landscape screen in a vertical frame): the part
        that exists, scaled the same, on dark bars."""
        lay = self.lay
        x0, y0, x1, y1 = rect
        sx, sy = lay.inner_w / (x1 - x0), lay.inner_h / (y1 - y0)
        ix0, iy0, ix1, iy1 = max(0.0, x0), max(0.0, y0), min(float(src.width), x1), min(float(src.height), y1)
        view = Image.new("RGB", (lay.inner_w, lay.inner_h), BARS)
        if ix1 > ix0 and iy1 > iy0:
            left, top = round((ix0 - x0) * sx), round((iy0 - y0) * sy)
            w = max(1, min(lay.inner_w - left, round((ix1 - ix0) * sx)))
            h = max(1, min(lay.inner_h - top, round((iy1 - iy0) * sy)))
            view.paste(src.resize((w, h), Image.Resampling.BICUBIC, box=(ix0, iy0, ix1, iy1)), (left, top))
        return view


BARS = (17, 17, 17)  # what fills a view where there is no picture


def prefetch(items, depth: int = 6):
    """Runs a generator in a thread, `depth` items ahead: decoding the next frames (reading ffmpeg's pipe,
    which releases the GIL) overlaps drawing this one. Errors are raised here, in order."""
    q: queue.Queue = queue.Queue(maxsize=depth)
    done = object()
    stop = threading.Event()

    def work():
        try:
            for item in items:
                while not stop.is_set():
                    try:
                        q.put(item, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if stop.is_set():
                    break
            q.put(done)
        except BaseException as e:  # noqa: BLE001 - handed to the consumer
            q.put(e)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        while True:
            item = q.get()
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()  # the consumer stopped early: let the worker end (and close the generator, which stops ffmpeg)
        while thread.is_alive():
            with contextlib.suppress(queue.Empty):
                q.get(timeout=0.1)
        thread.join()
        close = getattr(items, "close", None)
        if close:
            close()


def render(path, out, info: VideoInfo, camera: Camera, style: Style, out_width: int | None = None, progress=None,
           crf: int = 18, preset: str = "medium", timeline: Timeline | None = None) -> int:
    """Writes the polished video to `out` (MP4) and returns the number of frames written. It is written next
    to `out` under another name and renamed when complete: a failed or interrupted run leaves no half file,
    and does not replace a good one. With a timeline, the sped-up stretches keep only the frames they need."""
    lay = layout(info, style, out_width)
    comp = Compositor(info, style, lay)
    out = Path(out)
    partial = out.with_name(f".{out.name}.partial")
    graph = timeline.audio_graph(video_start=info.video_start) if timeline and timeline.sped and info.has_audio else None
    enc = Encoder(partial, lay.out_w, lay.out_h, info.fps, audio_from=path if info.has_audio else None, crf=crf, preset=preset,
                  audio_graph=graph, video_start=info.video_start)
    n = 0
    clock = 0.0  # output seconds so far
    try:
        # A few frames ahead, about 64 MB of them: 6 at 1080p, 2 at 4K (25 MB a frame).
        depth = max(2, min(6, (64 << 20) // (info.width * info.height * 3)))
        for i, f in enumerate(prefetch(frames(path, info.width, info.height, info.fps), depth=depth)):
            t = i / info.fps
            clock += 1 / (info.fps * (timeline.speed_at(t) if timeline else 1.0))
            want = round(clock * info.fps)  # output frames that should exist by now
            if want > n:
                frame = comp.frame(Image.fromarray(f, "RGB"), camera.rect(t))
                while n < want:
                    enc.write(frame)
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
