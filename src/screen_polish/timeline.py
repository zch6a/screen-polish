"""Speeding up the idle stretches: where nothing on screen changes for a while (waiting for a build, a page
loading, reading), the video plays faster.

A moment is idle when no cell changed in a way that counts (a blinking caret or a spinner that returns to
the same picture does not count: see analyze.py) and the whole screen did not change. A run of idle moments
at least `min_idle` long becomes a speed-up, keeping `margin` seconds at normal speed at each end so that
the cut in and out of the fast part is not abrupt. The sound in a sped-up stretch is sped up with it
(ffmpeg's atempo, which keeps the pitch): narration over a still screen would be fast too, which is why this
is only done when asked for.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass

import numpy as np

from .analyze import GLOBAL, STILL_MOTION, Activity


@dataclass
class Speedup:
    start: float  # seconds of the source
    end: float
    factor: float  # 4.0: four times as fast

    def to_dict(self) -> dict:
        return {k: round(v, 3) for k, v in asdict(self).items()}


def idle_stretches(act: Activity, factor: float = 4.0, min_idle: float = 2.0, margin: float = 0.5) -> list[Speedup]:
    if not len(act.times) or factor <= 1:
        return []
    idle = ~act.grids.reshape(len(act.times), -1).any(axis=1) & (act.share <= GLOBAL)
    if act.motion is not None:  # a pointer circling one spot only reverts, but it is moving: not still
        idle &= act.motion <= STILL_MOTION
    step = float(np.median(np.diff(act.times))) if len(act.times) > 1 else 0.125
    out: list[Speedup] = []
    run_start = None
    for t, quiet in zip(np.append(act.times, act.times[-1] + step), np.append(idle, False), strict=True):
        if quiet and run_start is None:
            run_start = float(t)
        elif not quiet and run_start is not None:
            start, end = run_start + margin, float(t) - step - margin  # the last idle moment, less the margin
            if end - start >= min_idle - 2 * margin and end > start:
                out.append(Speedup(round(start, 3), round(end, 3), factor))
            run_start = None
    return out


class Timeline:
    """Source time to output time and back, for a list of speed-ups (sorted, not overlapping)."""

    def __init__(self, speedups: list[Speedup], duration: float, fps: float | None = None):
        """With `fps`, each speed-up starts on a frame and lasts a whole number of output frames: the video can only
        change speed between frames, and the sound is cut at the same instants, so they stay in step (unsnapped,
        each boundary could put them a frame apart, and the errors add up)."""
        self.duration = duration
        self.parts: list[Speedup] = []
        t = 0.0
        for s in sorted(speedups, key=lambda s: s.start):
            start, end = max(s.start, t), min(s.end, duration)
            if fps and s.factor > 0:
                start = math.ceil(start * fps - 1e-6) / fps
                end = start + math.floor((end - start) * fps / s.factor + 1e-6) * s.factor / fps
            if end <= start or s.factor <= 0:
                continue
            if start > t:
                self.parts.append(Speedup(t, start, 1.0))
            self.parts.append(Speedup(start, end, s.factor))
            t = end
        if t < duration:
            self.parts.append(Speedup(t, duration, 1.0))
        self._starts = [p.start for p in self.parts]

    @property
    def sped(self) -> bool:
        return any(p.factor != 1.0 for p in self.parts)

    def speed_at(self, t: float) -> float:
        k = bisect.bisect_right(self._starts, t) - 1  # once per frame: no scan of every part
        if 0 <= k < len(self.parts) and t < self.parts[k].end:
            return self.parts[k].factor
        return 1.0

    def out_duration(self) -> float:
        return sum((p.end - p.start) / p.factor for p in self.parts)

    def audio_graph(self, source: str = "1:a:0", out: str = "aout", video_start: float = 0.0) -> str:
        """An ffmpeg filter graph that puts the sound on the video's clock, cuts it into the same parts, speeds
        up the fast ones, and joins them, then pads with silence (so a short sound does not end the video)."""
        from .media import audio_align

        n = len(self.parts)
        chains = [f"[{source}]{audio_align(video_start)},asplit={n}" + "".join(f"[s{i}]" for i in range(n))]  # one input, one filter
        labels = []
        for i, p in enumerate(self.parts):
            tempo = "".join(f",atempo={f:g}" for f in _atempo_chain(p.factor)) if p.factor != 1.0 else ""
            chains.append(f"[s{i}]atrim=start={p.start:.3f}:end={p.end:.3f},asetpts=PTS-STARTPTS{tempo}[a{i}]")
            labels.append(f"[a{i}]")
        chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1,apad[{out}]")
        return ";".join(chains)


def protect(speedups: list[Speedup], keep: list[tuple[float, float]], min_length: float = 1.0) -> list[Speedup]:
    """The speed-ups with the `keep` intervals (the camera's moves) taken out: a zoom squeezed into a quarter of
    its time is a jolt. What is left of a speed-up must still be `min_length` long."""
    out = []
    for s in speedups:
        pieces = [(s.start, s.end)]
        for a, b in keep:
            pieces = [p for lo, hi in pieces for p in ((lo, min(hi, a)), (max(lo, b), hi)) if p[1] > p[0]]
        out += [Speedup(round(lo, 3), round(hi, 3), s.factor) for lo, hi in pieces if hi - lo >= min_length]
    return out


def _atempo_chain(factor: float) -> list[float]:
    """atempo takes 0.5 to 2.0 in older ffmpeg builds (100 in newer ones): chain factors of 2 to be safe."""
    out = []
    while factor > 2.0:
        out.append(2.0)
        factor /= 2.0
    out.append(factor)
    return out
