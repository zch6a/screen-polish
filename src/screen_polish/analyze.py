"""Where things happen on screen, and when: from frame differences to a zoom plan.

The video is decoded small (320 px wide, 8 frames a second, gray) and each frame is compared with the
one before. Changed pixels are pooled into 8x8 cells, so that compression noise does not count. Not
every change means "look here", so each changed cell is sorted:

- **reverting**: the cell went back to a state it had in the last 2 seconds. A blinking caret, the
  mouse moving off something, a hover effect ending. Ignored.
- **persistent**: a new state that is still there half a second later. Typed text, a menu or dialog
  that opened, a value that changed. Only these can start a zoom ("anchors").
- **transient**: the rest (the mouse passing over, a highlight moving through a menu). They keep a zoom
  going where anchors started it, but never start one: a wandering mouse is no reason to zoom.

A moment where a large part of the screen changes (scrolling, a new page, switching windows) is
"global": no zoom across it, the viewer needs the whole picture. In sliding windows of 1.5 s the
activity gives a box: where to look. Consecutive windows whose boxes stay in the same area, and still
fit in one zoom, form a segment; a segment long and small enough becomes a zoom into its box (with a
margin, the video's aspect ratio, and a limit on how far). Typing along a wide field becomes several
zooms one after the other, which the camera pans through.

Clicks, if a log of them is given, count as anchors at their point.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

import numpy as np

from .media import VideoInfo, frames

ANALYSIS_WIDTH = 640  # at most; a 1080p screen's 14 px text still has strokes at a third of its size
ANALYSIS_FPS = 8
NOISE = 24  # a gray level change below this is compression noise
CELL = 8  # analysis pixels per cell side
CELL_ACTIVE = 0.05  # share of a cell's pixels that must change for it to count
GLOBAL = 0.30  # share of changed cells above which the whole screen changed
MEMORY = 2.0  # seconds: a cell returning to a state seen this recently is reverting
PERSIST = 0.5  # seconds: a new state still there this much later is persistent
UNDO = 2.0  # seconds: a change undone within this long (the mouse resting on something, then moving on) is no anchor
WINDOW, HOP = 1.5, 0.5  # seconds
NEAR = 0.25  # a box this close to its segment (centre distance, share of the frame) continues it
MIN_SEGMENT = 1.0  # seconds of activity for a zoom
MIN_GAIN = 1.25  # zooms smaller than this are not worth the motion
KEEP_ALIVE = 3.0  # seconds: passing changes keep a zoom going this long after its last persistent one
EARLY_ANCHORS = 8  # cells: in the first MEMORY seconds there is no history to recognise reverting changes, and in
#                    the last UNDO seconds no future to see a change undone, so a window there needs this many anchor
#                    cells in all (typing adds up; the mouse leaving where it started, or resting at the end, does not)
SETTLE = 0.5  # seconds: nothing in the first half second starts a zoom (seen: the first frames of an encode differing
#              in a hundred scattered cells with one build of ffmpeg; and recorders' countdowns disappear then)
SEGMENT_ANCHORS = 3  # cells: what a zoom needs in all, so that one pointer-sized blip cannot start one


@dataclass
class Activity:
    times: np.ndarray  # (T,) seconds
    grids: np.ndarray  # (T, rows, cols) bool: cells with a change that is not reverting
    anchors: np.ndarray  # (T, rows, cols) bool: of those, the persistent ones
    share: np.ndarray  # (T,) share of cells that changed at all: is the whole screen changing?
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
    """Streams the video once, keeping only the last few seconds of small frames in memory."""
    width = max(CELL, min(width, info.width) // 2 * 2)  # never larger than the video itself
    height = max(CELL, round(width * info.height / info.width / 2) * 2)
    return classify(frames(path, width, height, fps, gray=True), fps, info.width, info.height)


def classify(stream, fps: float, width: int, height: int) -> Activity:
    """The sorting described above, over a stream of small gray frames at `fps`."""
    back = max(2, round(MEMORY * fps))
    persist, undo = max(1, round(PERSIST * fps)), max(1, round(UNDO * fps))
    ahead = max(persist, undo)
    buf: deque[np.ndarray] = deque(maxlen=back + 1 + ahead)
    times, grids, anchors, share = [], [], [], []
    rows = cols = 0
    count = 0

    def judge(j: int, frames_: list[np.ndarray], pos: int) -> None:
        """Frame number j, at position pos in frames_ (the ones before it, and up to `ahead` after)."""
        f, prev = frames_[pos], frames_[pos - 1]
        changed = same_cells(f, prev, rows, cols, invert=True)
        fresh = np.zeros_like(changed)
        anchor = np.zeros_like(changed)
        idx = np.nonzero(changed)
        if len(idx[0]):  # only the cells that changed are compared with the past and the future: few, usually
            now, before = blocks(f, idx, rows, cols), blocks(prev, idx, rows, cols)
            reverting = np.zeros(len(idx[0]), bool)
            for old in frames_[: pos - 1]:
                reverting |= same_blocks(now, blocks(old, idx, rows, cols))
            persists = same_blocks(blocks(frames_[min(len(frames_) - 1, pos + persist)], idx, rows, cols), now)
            undone = np.zeros(len(idx[0]), bool)
            for later in frames_[pos + 1: pos + 1 + undo]:
                undone |= same_blocks(blocks(later, idx, rows, cols), before)
            fresh[idx] = ~reverting
            anchor[idx] = ~reverting & persists & ~undone
            if pos + undo >= len(frames_) and anchor.sum() < EARLY_ANCHORS:
                anchor[:] = False  # the end of the video: no future to see it undone, so only a big change counts
            if j / fps < SETTLE:
                anchor[:] = False  # the recording settling: the recorder's own countdown leaving, the encoder's first frames
        times.append(j / fps)
        grids.append(fresh)
        anchors.append(anchor)
        share.append(float(changed.mean()))

    for f in stream:
        if not rows:
            rows, cols = f.shape[0] // CELL, f.shape[1] // CELL
        buf.append(f)
        count += 1
        j = count - 1 - ahead  # the frame judged now: `ahead` frames after it are known
        if j >= 1:
            judge(j, list(buf), len(buf) - 1 - ahead)
    frames_ = list(buf)  # the last `ahead` frames: their future is the end of the video
    first = count - len(frames_)  # frame number of frames_[0]
    for j in range(max(1, count - ahead), count):
        judge(j, frames_, j - first)
    shape = (0, max(rows, 1), max(cols, 1))
    g = np.array(grids, bool) if grids else np.zeros(shape, bool)
    a = np.array(anchors, bool) if anchors else np.zeros(shape, bool)
    return Activity(np.array(times, float), g, a, np.array(share, float), width, height)


def same_cells(a: np.ndarray, b: np.ndarray, rows: int, cols: int, invert: bool = False) -> np.ndarray:
    """Cells where two frames agree (or, with invert, differ), allowing for compression noise."""
    changed = np.abs(a.astype(np.int16) - b.astype(np.int16)) > NOISE
    differs = changed[: rows * CELL, : cols * CELL].reshape(rows, CELL, cols, CELL).mean(axis=(1, 3)) > CELL_ACTIVE
    return differs if invert else ~differs


def blocks(img: np.ndarray, idx, rows: int, cols: int) -> np.ndarray:
    """The pixels of the cells at idx (row and column arrays), as (n, CELL, CELL) int16."""
    grid = img[: rows * CELL, : cols * CELL].reshape(rows, CELL, cols, CELL).transpose(0, 2, 1, 3)
    return grid[idx].astype(np.int16)


def same_blocks(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (np.abs(a - b) > NOISE).mean(axis=(1, 2)) <= CELL_ACTIVE


def add_clicks(act: Activity, clicks: list[dict]) -> None:
    """Clicks ({"t", "x", "y"} in source pixels) as anchors: a 3x3 block of cells around the point, for half
    a second, so that a click alone is enough for a zoom when the screen hardly changes."""
    rows, cols = act.shape
    for c in clicks:
        r = int(np.clip(c["y"] / act.height * rows, 0, rows - 1))
        k = int(np.clip(c["x"] / act.width * cols, 0, cols - 1))
        near = (act.times >= c["t"]) & (act.times <= c["t"] + 0.5)
        for g in (act.grids, act.anchors):
            g[near, max(0, r - 1): r + 2, max(0, k - 1): k + 2] = True


def plan(act: Activity, max_zoom: float = 2.0) -> list[Zoom]:
    """The zooms for this activity, in time order, not overlapping."""
    if not len(act.times):
        return []
    rows, cols = act.shape
    is_global = act.share > GLOBAL
    duration = float(act.times[-1])
    segments: list[dict] = []
    current: dict | None = None
    t = 0.0
    while t <= duration:
        inside = (act.times >= t) & (act.times < t + WINDOW)
        box = None
        early = inside & (act.times < MEMORY)  # no history yet: these count only when they add up
        found = int(act.anchors[inside & ~early].sum())
        found_early = int(act.anchors[early].sum())
        anchored = found + (found_early if found_early >= EARLY_ANCHORS else 0) >= 1
        if inside.any() and not is_global[inside].any():
            # Where: the persistent changes when there are some (stray noise elsewhere does not stretch the box),
            # else everything that changed (a zoom kept going by passing changes follows them).
            box = (_box(act.anchors[inside], rows, cols) if anchored else None) or _box(act.grids[inside], rows, cols)
        if anchored and current is not None:
            current["anchor"] = t + WINDOW
        alive = current is not None and t - current.get("anchor", current["start"] + WINDOW) < KEEP_ALIVE
        if box is None or not (anchored or (alive and _near(box, current["box"]))):
            current = None  # quiet, only passing changes, or the whole screen changed: the segment ends here
        else:
            union = _union(box, current["box"]) if current else box
            if current and _near(box, current["box"]) and _area(union) < 0.5 and _zoom_rect(union, act.width, act.height, max_zoom):
                current["box"] = union
                current["end"] = t + WINDOW
            elif anchored:
                current = {"start": t, "end": t + WINDOW, "box": box, "anchor": t + WINDOW}  # elsewhere, or too big for one zoom
                segments.append(current)
            else:
                current = None
        t += HOP
    zooms: list[Zoom] = []
    for s in segments:
        start, end = _trim(act, s["start"], s["end"])
        if zooms:  # windows overlap, so a segment can begin inside the last one: it starts where that ends
            start = max(start, zooms[-1].end)
        span = (act.times >= s["start"]) & (act.times < s["end"])  # counted once each: windows overlap
        if end - start < MIN_SEGMENT or int(act.anchors[span].sum()) < SEGMENT_ANCHORS:
            continue
        z = _zoom_rect(s["box"], act.width, act.height, max_zoom)
        if z:
            zooms.append(Zoom(start, end, *z))
    return _merge(zooms, act.width, act.height, max_zoom)


def wide_moments(act: Activity, gap: float = 1.0) -> list[float]:
    """When the whole screen changed (a new page, a window switch): the camera shows all of it then, so
    the viewer sees where they are. One time per burst of change."""
    out: list[float] = []
    for t in act.times[act.share > GLOBAL]:
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


def _merge(zooms: list[Zoom], width: int, height: int, max_zoom: float, gap: float = 1.0) -> list[Zoom]:
    """Neighbouring zooms on nearly the same place become one (no zooming out and back in for a pause),
    if together they still make a zoom; otherwise the camera pans from one to the other."""
    out: list[Zoom] = []
    for z in zooms:
        if out and z.start - out[-1].end < gap and _overlap(out[-1], z) > 0.5:
            p = out[-1]
            box = (min(p.x, z.x) / width, min(p.y, z.y) / height, max(p.x + p.w, z.x + z.w) / width, max(p.y + p.h, z.y + z.h) / height)
            # the rectangles already have their margins: fit them as they are
            x0, y0, x1, y1 = box[0] * width, box[1] * height, box[2] * width, box[3] * height
            aspect = width / height
            w = max(x1 - x0, (y1 - y0) * aspect, width / max_zoom)
            if w <= width / MIN_GAIN:
                h = w / aspect
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                out[-1] = Zoom(p.start, z.end, float(np.clip(cx - w / 2, 0, width - w)), float(np.clip(cy - h / 2, 0, height - h)), w, h, p.reason)
                continue
        out.append(z)
    return out


def _overlap(a: Zoom, b: Zoom) -> float:
    """Intersection over the smaller rectangle."""
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return ix * iy / min(a.w * a.h, b.w * b.h)
