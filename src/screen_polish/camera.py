"""From zooms to where the camera is at each moment.

The camera arrives a little before the activity starts and leaves a little after it ends. Each move
takes TRANSITION seconds and eases in and out. Between two zooms that are close in time it pans
directly instead of zooming out and back in. Moves are interpolated in the centre and in the logarithm
of the size, so a 2x zoom feels as fast at its start as at its end.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TRANSITION = 0.8  # seconds per move
LEAD = 0.3  # arrive this long before the activity
TAIL = 0.8  # stay this long after it
HOLD_WIDE = 0.6  # after the whole screen changed, show all of it this long before zooming in again


@dataclass(frozen=True)
class View:
    cx: float  # centre, source pixels
    cy: float
    w: float  # width shown, source pixels; the height follows from the aspect ratio


def ease(p: float) -> float:
    """Smootherstep: no jolt at either end."""
    p = min(1.0, max(0.0, p))
    return p * p * p * (p * (6 * p - 15) + 10)


class Camera:
    def __init__(self, zooms, width: int, height: int, duration: float, transition: float = TRANSITION,
                 wide: list[float] | None = None):
        """`wide`: moments when the whole screen changed; the camera shows all of it across them, even
        between two zooms close in time."""
        self.width, self.height, self.t = width, height, transition
        self.aspect = width / height
        full = View(width / 2, height / 2, float(width))
        shots: list[tuple[float, View]] = [(0.0, full)]
        leave: float | None = None  # when the current zoom would start zooming out
        wide = sorted(wide or [])
        for z in zooms:
            view = self._clamp(View(z.x + z.w / 2, z.y + z.h / 2, z.w))
            arrive = max(0.0, z.start - LEAD - transition)
            cut = next((w for w in wide if leave is not None and leave - transition < w < z.start), None)
            if cut is not None:
                # Out to the whole screen by the time it changes (or as soon as the zoom allows), a moment there, then in.
                out_at = max(shots[-1][0] + transition, min(leave, cut - transition))
                shots.append((out_at, full))
                arrive = max(arrive, out_at + transition + HOLD_WIDE)
            elif leave is not None and arrive >= leave + 2 * transition:
                shots.append((leave, full))  # enough time to zoom out and back in
            arrive = max(arrive, shots[-1][0] + transition)  # each move finishes before the next starts
            shots.append((arrive, view))
            leave = max(z.end + TAIL, arrive + transition)
        if leave is not None and zooms:
            # End on the whole screen: leave earlier if the video would stop mid-move (not before the activity ends).
            leave = max(min(leave, duration - transition), zooms[-1].end, shots[-1][0] + transition)
            if leave < duration:
                shots.append((leave, full))
        self.shots = shots

    def _clamp(self, v: View) -> View:
        w = min(max(v.w, 1.0), float(self.width))
        h = w / self.aspect
        cx = min(max(v.cx, w / 2), self.width - w / 2)
        cy = min(max(v.cy, h / 2), self.height - h / 2)
        return View(cx, cy, w)

    def at(self, t: float) -> View:
        k = 0
        for i, (start, _) in enumerate(self.shots):
            if start <= t:
                k = i
        start, target = self.shots[k]
        if k == 0 or t >= start + self.t:
            return target
        prev = self.shots[k - 1][1]
        p = ease((t - start) / self.t)
        w = math.exp(math.log(prev.w) + (math.log(target.w) - math.log(prev.w)) * p)
        cx = prev.cx + (target.cx - prev.cx) * p
        cy = prev.cy + (target.cy - prev.cy) * p
        return self._clamp(View(cx, cy, w))

    def rect(self, t: float) -> tuple[float, float, float, float]:
        """(x0, y0, x1, y1) in source pixels at time t."""
        v = self.at(t)
        h = v.w / self.aspect
        return (v.cx - v.w / 2, v.cy - h / 2, v.cx + v.w / 2, v.cy + h / 2)
