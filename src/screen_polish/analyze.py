"""Where things happen on screen, and when: from frame differences to a zoom plan.

The video is decoded small (320 px wide, 8 frames a second, gray) and each frame is compared with the
one before. Changed pixels are pooled into 8x8 cells, so that compression noise and a blinking caret
do not count, and each moment becomes a map of active cells. Then:

- a moment where a large part of the screen changes (scrolling, a new page, switching windows) is
  "global": no zoom across it, the viewer needs the whole picture;
- in sliding windows of 1.5 s, the active cells of the local moments give a box: where to look;
- consecutive windows whose boxes stay in the same area form a segment, and a segment long and small
  enough becomes a zoom into its box (with a margin, the video's aspect ratio, and a limit on how far).

Clicks, if a log of them is given, count as strong activity at their point.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .media import VideoInfo, frames

ANALYSIS_WIDTH = 320
ANALYSIS_FPS = 8
NOISE = 24  # a gray level change below this is compression noise
CELL = 8  # analysis pixels per cell side
CELL_ACTIVE = 0.05  # share of a cell's pixels that must change for it to count
GLOBAL = 0.30  # share of active cells above which the whole screen changed
WINDOW, HOP = 1.5, 0.5  # seconds
NEAR = 0.25  # a box this close to its segment (centre distance, share of the frame) continues it
MIN_SEGMENT = 1.0  # seconds of activity for a zoom
MIN_GAIN = 1.25  # zooms smaller than this are not worth the motion


@dataclass
class Activity:
    times: np.ndarray  # (T,) seconds
    grids: np.ndarray  # (T, rows, cols) bool: active cells
    width: int  # the source's size, to map cells back to pixels
    height: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.grids.shape[1], self.grids.shape[2]


@dataclass
class Zoom:
    start: float  # seconds: the activity it is for (the camera moves a little before and after)
    end: float
    x: float  # the source rectangle to show, in source pixels
    y: float
    w: float
    h: float
    reason: str = "activity"  # or "click", "manual"

    def to_dict(self) -> dict:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in asdict(self).items()}


def activity(path, info: VideoInfo, width: int = ANALYSIS_WIDTH, fps: float = ANALYSIS_FPS) -> Activity:
    height = max(CELL, round(width * info.height / info.width / 2) * 2)
    rows, cols = height // CELL, width // CELL
    times, grids, prev = [], [], None
    for i, f in enumerate(frames(path, width, height, fps, gray=True)):
        if prev is not None:
            times.append(i / fps)
            grids.append(cells(f, prev, rows, cols))
        prev = f
    g = np.array(grids, bool) if grids else np.zeros((0, rows, cols), bool)
    return Activity(np.array(times, float), g, info.width, info.height)


def cells(frame: np.ndarray, prev: np.ndarray, rows: int, cols: int) -> np.ndarray:
    changed = np.abs(frame.astype(np.int16) - prev.astype(np.int16)) > NOISE
    changed = changed[: rows * CELL, : cols * CELL].reshape(rows, CELL, cols, CELL)
    return changed.mean(axis=(1, 3)) > CELL_ACTIVE


def add_clicks(act: Activity, clicks: list[dict]) -> None:
    """Clicks ({"t", "x", "y"} in source pixels) as activity: a 3x3 block of cells around the point, for half
    a second, so that a click alone is enough for a zoom when the screen hardly changes."""
    rows, cols = act.shape
    for c in clicks:
        r = int(np.clip(c["y"] / act.height * rows, 0, rows - 1))
        k = int(np.clip(c["x"] / act.width * cols, 0, cols - 1))
        near = (act.times >= c["t"]) & (act.times <= c["t"] + 0.5)
        act.grids[near, max(0, r - 1): r + 2, max(0, k - 1): k + 2] = True


def plan(act: Activity, max_zoom: float = 2.0) -> list[Zoom]:
    """The zooms for this activity, in time order, not overlapping."""
    if not len(act.times):
        return []
    rows, cols = act.shape
    share = act.grids.reshape(len(act.times), -1).mean(axis=1)
    is_global = share > GLOBAL
    duration = float(act.times[-1])
    segments: list[dict] = []
    current: dict | None = None
    t = 0.0
    while t <= duration:
        inside = (act.times >= t) & (act.times < t + WINDOW)
        box = None
        if inside.any() and not is_global[inside].any():
            box = _box(act.grids[inside], rows, cols)
        if box is None:
            current = None  # quiet, or the whole screen changed: the segment ends here
        else:
            if current and _near(box, current["box"]) and _area(_union(box, current["box"])) < 0.5:
                current["box"] = _union(box, current["box"])
                current["end"] = t + WINDOW
            else:
                current = {"start": t, "end": t + WINDOW, "box": box}
                segments.append(current)
        t += HOP
    zooms = []
    for s in segments:
        start, end = _trim(act, s["start"], s["end"])
        if end - start < MIN_SEGMENT:
            continue
        z = _zoom_rect(s["box"], act.width, act.height, max_zoom)
        if z:
            zooms.append(Zoom(start, end, *z))
    return _merge(zooms)


def wide_moments(act: Activity, gap: float = 1.0) -> list[float]:
    """When the whole screen changed (a new page, a window switch): the camera shows all of it then, so
    the viewer sees where they are. One time per burst of change."""
    if not len(act.times):
        return []
    share = act.grids.reshape(len(act.times), -1).mean(axis=1)
    out: list[float] = []
    for t in act.times[share > GLOBAL]:
        if not out or t - out[-1] > gap:
            out.append(round(float(t), 3))
    return out


def _box(grids: np.ndarray, rows: int, cols: int):
    """Where the active cells of these moments are, as (x0, y0, x1, y1) shares of the frame; the 5th to
    95th percentile of their positions, so that a stray cell elsewhere does not stretch it."""
    ys, xs = np.nonzero(grids.sum(axis=0))
    weights = grids.sum(axis=0)[ys, xs]
    if weights.sum() < 2:
        return None
    rx = np.repeat(xs, weights)
    ry = np.repeat(ys, weights)
    x0, x1 = np.percentile(rx, [5, 95])
    y0, y1 = np.percentile(ry, [5, 95])
    return (x0 / cols, y0 / rows, (x1 + 1) / cols, (y1 + 1) / rows)


def _near(a, b) -> bool:
    ca = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    cb = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    return max(abs(ca[0] - cb[0]), abs(ca[1] - cb[1])) < NEAR


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _trim(act: Activity, start: float, end: float) -> tuple[float, float]:
    """The first and last moment with activity in this segment."""
    inside = (act.times >= start) & (act.times < end)
    active = act.grids[inside].reshape(int(inside.sum()), -1).any(axis=1)
    ts = act.times[inside][active]
    return (float(ts[0]), float(ts[-1])) if len(ts) else (start, start)


def _zoom_rect(box, width: int, height: int, max_zoom: float):
    """The source rectangle for a box: a margin around it, the video's aspect ratio, no closer than
    max_zoom, inside the frame. None when it would hardly be a zoom."""
    x0, y0, x1, y1 = box[0] * width, box[1] * height, box[2] * width, box[3] * height
    bw, bh = x1 - x0, y1 - y0
    margin = 0.15
    bw, bh = bw * (1 + 2 * margin) + 0.02 * width, bh * (1 + 2 * margin) + 0.02 * height
    aspect = width / height
    w = max(bw, bh * aspect, width / max_zoom)
    h = w / aspect
    if w > width / MIN_GAIN:
        return None
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    x = float(np.clip(cx - w / 2, 0, width - w))
    y = float(np.clip(cy - h / 2, 0, height - h))
    return x, y, float(w), float(h)


def _merge(zooms: list[Zoom], gap: float = 1.0) -> list[Zoom]:
    """Neighbouring zooms on nearly the same place become one: no zooming out and back in for a pause."""
    out: list[Zoom] = []
    for z in zooms:
        if out and z.start - out[-1].end < gap and _overlap(out[-1], z) > 0.5:
            p = out[-1]
            x0, y0 = min(p.x, z.x), min(p.y, z.y)
            x1, y1 = max(p.x + p.w, z.x + z.w), max(p.y + p.h, z.y + z.h)
            aspect = p.w / p.h
            w = max(x1 - x0, (y1 - y0) * aspect)
            out[-1] = Zoom(p.start, z.end, x0, y0, w, w / aspect, p.reason)
        else:
            out.append(z)
    return out


def _overlap(a: Zoom, b: Zoom) -> float:
    """Intersection over the smaller rectangle."""
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return ix * iy / min(a.w * a.h, b.w * b.h)
