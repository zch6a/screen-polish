"""screen-polish: make a screen recording you already have look produced.

    screen-polish demo.mp4                       zoom where things happen, frame it, write demo.polished.mp4
    screen-polish demo.mp4 -o demo.gif           the same as a GIF
    screen-polish demo.mp4 --plan-out plan.json  only work out the zooms, to look at or edit
    screen-polish demo.mp4 --plan plan.json      use those zooms
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .analyze import Zoom, activity, add_clicks, plan, wide_moments
from .camera import Camera
from .media import MediaError, probe, to_gif
from .render import BACKGROUNDS, Style, render
from .timeline import Speedup, Timeline, idle_stretches

PLAN_VERSION = 1


def plan_to_json(zooms: list[Zoom], info, source: str, wide: list[float] | None = None, speedups: list[Speedup] | None = None) -> dict:
    """`wide`: the moments the whole screen changed, when the camera shows all of it; `speed`: the stretches
    played faster (start, end in source seconds, and factor)."""
    return {"version": PLAN_VERSION, "source": {"file": Path(source).name, "width": info.width, "height": info.height,
                                                "duration": round(info.duration, 3)},
            "zooms": [z.to_dict() for z in zooms], "wide": list(wide or []), "speed": [s.to_dict() for s in speedups or []]}


def _number(value, what: str, positive: bool = False) -> float:
    """A finite number from JSON (not a string, not NaN or Infinity), or a plain error."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a number, not {value!r}")
    if positive and value <= 0:
        raise ValueError(f"{what} must be more than 0, not {value!r}")
    return float(value)


def plan_from_json(data, info) -> tuple[list[Zoom], list[float]]:
    """Zooms (and wide moments) from a plan file; a plan made for another size is scaled to this one."""
    if not isinstance(data, dict):
        raise ValueError("a plan is a JSON object with \"version\", \"source\" and \"zooms\"")
    if data.get("version") != PLAN_VERSION:
        raise ValueError(f"plan version {data.get('version')!r} is not {PLAN_VERSION}")
    src = data.get("source") or {}
    if not isinstance(src, dict):
        raise ValueError("\"source\" in the plan must be an object with the video's width and height")
    sx = info.width / _number(src.get("width", info.width), "source width", positive=True)
    sy = info.height / _number(src.get("height", info.height), "source height", positive=True)
    items = data.get("zooms") or []
    if not isinstance(items, list):
        raise ValueError("\"zooms\" in the plan must be a list")
    zooms = []
    for i, z in enumerate(items, 1):
        if not isinstance(z, dict):
            raise ValueError(f"zoom {i} in the plan must be an object with start, end, x, y, w, h")
        try:
            v = {k: _number(z.get(k), f"zoom {i}: {k}", positive=k in ("w", "h")) for k in ("start", "end", "x", "y", "w", "h")}
        except ValueError as e:
            raise ValueError(f"{e} (each zoom needs start, end, x, y, w, h)") from None
        if v["end"] <= v["start"]:
            raise ValueError(f"zoom {i} in the plan: end must be after start")
        zooms.append(Zoom(v["start"], v["end"], v["x"] * sx, v["y"] * sy, v["w"] * sx, v["h"] * sy, str(z.get("reason", "manual"))))
    wide = data.get("wide") or []
    if not isinstance(wide, list):
        raise ValueError("\"wide\" in the plan must be a list of times in seconds")
    return sorted(zooms, key=lambda z: z.start), sorted(_number(w, "a time in \"wide\"") for w in wide)


def speedups_from_json(data) -> list[Speedup]:
    """The "speed" list of a plan: stretches played faster, factor 1 to 16."""
    items = data.get("speed") or [] if isinstance(data, dict) else []
    if not isinstance(items, list):
        raise ValueError("\"speed\" in the plan must be a list of {\"start\", \"end\", \"factor\"}")
    out = []
    for i, s in enumerate(items, 1):
        if not isinstance(s, dict):
            raise ValueError(f"speed-up {i} in the plan must be an object with start, end, factor")
        v = {k: _number(s.get(k), f"speed-up {i}: {k}") for k in ("start", "end", "factor")}
        if v["end"] <= v["start"] or not 1 <= v["factor"] <= 16:
            raise ValueError(f"speed-up {i} in the plan: end must be after start, and the factor between 1 and 16")
        out.append(Speedup(v["start"], v["end"], v["factor"]))
    return sorted(out, key=lambda s: s.start)


def load_clicks(path: str) -> list[dict]:
    """A click log: {"clicks": [{"t": seconds, "x": px, "y": px}, ...]} or just the list."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    clicks = data.get("clicks", []) if isinstance(data, dict) else data
    if not isinstance(clicks, list):
        raise ValueError(f"{path}: the clicks must be a list of {{\"t\", \"x\", \"y\"}}")
    out = []
    for i, c in enumerate(clicks, 1):
        if not isinstance(c, dict):
            raise ValueError(f"{path}: click {i} must be an object with t, x, y")
        out.append({k: _number(c.get(k), f"{path}: click {i}: {k}") for k in ("t", "x", "y")})
    return out


def _default_out(src: Path, gif: bool = False) -> Path:
    return src.with_name(src.stem + ".polished" + (".gif" if gif else ".mp4"))


def _progress(duration: float):
    last = [0.0]
    start = time.monotonic()

    def show(t: float) -> None:
        now = time.monotonic()
        if now - last[0] < 0.5 and t < duration - 0.05:
            return
        last[0] = now
        pct = min(100, int(100 * t / duration)) if duration else 0
        print(f"\r  rendering {pct:3d}%  ({t:.1f} of {duration:.1f} s, {now - start:.0f} s elapsed)", end="", file=sys.stderr, flush=True)

    return show


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="screen-polish", description="Zoom into where things happen in a screen recording, and frame "
                                "it on a background. Works on recordings you already have: no special recorder needed.",
                                epilog="Everything runs on this computer; nothing is uploaded.")
    p.add_argument("input", help="the screen recording (MP4, MOV, MKV, WebM ...)")
    p.add_argument("-o", "--output", help="the result: .mp4 or .gif (default: INPUT.polished.mp4)")
    p.add_argument("--plan", metavar="JSON", help="use the zooms in this plan instead of finding them")
    p.add_argument("--plan-out", metavar="JSON", help="write the zooms found to this file (and render nothing, unless -o is given)")
    p.add_argument("--clicks", metavar="JSON", help="a click log ({\"clicks\": [{\"t\", \"x\", \"y\"}]}) to zoom on, besides the activity")
    p.add_argument("--max-zoom", type=float, default=2.0, help="how far to zoom in at most (default 2.0)")
    p.add_argument("--no-zoom", action="store_true", help="only frame the video")
    p.add_argument("--speed-idle", type=float, metavar="FACTOR", help="play stretches where nothing changes this many times as fast "
                   "(e.g. 4); their sound too, pitch kept")
    p.add_argument("--idle-min", type=float, default=2.0, metavar="SECONDS", help="how long a stretch must be still to be sped up (default 2)")
    p.add_argument("--background", default="midnight", help=f"{', '.join(BACKGROUNDS)}, or a colour #rrggbb (default midnight)")
    p.add_argument("--padding", type=float, default=6.0, help="space around the video, %% of the width (default 6)")
    p.add_argument("--radius", type=float, default=1.2, help="corner radius, %% of the width (default 1.2)")
    p.add_argument("--no-shadow", action="store_true")
    p.add_argument("--no-frame", action="store_true", help="no background, corners or shadow: the zoomed video edge to edge")
    p.add_argument("--width", type=int, help="output width in pixels (default: the input's)")
    p.add_argument("--quality", choices=["draft", "good", "best"], default="good", help="encoding speed and size (default good)")
    p.add_argument("--version", action="version", version=f"screen-polish {__version__}")
    args = p.parse_args(argv)

    try:
        src = Path(args.input)
        info = probe(src)
        if not 0 <= args.padding <= 40:
            raise ValueError("--padding is a share of the width: 0 to 40")
        if not 0 <= args.radius <= 20:
            raise ValueError("--radius is a share of the width: 0 to 20")
        if args.width is not None and not 16 <= args.width <= 7680:
            raise ValueError("--width must be between 16 and 7680 pixels")
        if not 1.0 <= args.max_zoom <= 8.0:
            raise ValueError("--max-zoom must be between 1 and 8")
        if args.speed_idle is not None and not 1.0 < args.speed_idle <= 16.0:
            raise ValueError("--speed-idle must be more than 1 and at most 16")
        if not 1.0 <= args.idle_min <= 600:
            raise ValueError("--idle-min must be between 1 and 600 seconds")
        style = Style(background=args.background, padding=args.padding / 100, radius=args.radius / 100,
                      shadow=not args.no_shadow, frame=not args.no_frame)
        if style.background not in BACKGROUNDS:
            from .render import _color

            _color(style.background)  # a bad colour is said now, not after the analysis
        print(f"{src.name}: {info.width}x{info.height}, {info.fps:g} fps, {info.duration:.1f} s"
              f"{', with audio' if info.has_audio else ''}", file=sys.stderr)

        wide: list[float] = []
        zooms: list[Zoom] = []
        speedups: list[Speedup] = []
        if args.plan:
            data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            zooms, wide = plan_from_json(data, info)
            speedups = speedups_from_json(data)
            if args.no_zoom:
                zooms = []
        elif not args.no_zoom or args.speed_idle:
            print("  finding where things happen ...", file=sys.stderr)
            act = activity(src, info)
            wide = wide_moments(act)
            if args.speed_idle:
                speedups = idle_stretches(act, factor=args.speed_idle, min_idle=args.idle_min)  # before the clicks are added
            if args.clicks:
                add_clicks(act, load_clicks(args.clicks))
            if not args.no_zoom:
                zooms = plan(act, max_zoom=max(1.0, args.max_zoom))
        for z in zooms:
            print(f"  zoom {z.start:6.1f}-{z.end:6.1f} s  {info.width / z.w:.1f}x on ({z.x:.0f}, {z.y:.0f}) {z.w:.0f}x{z.h:.0f}", file=sys.stderr)
        if not zooms and not args.no_zoom:
            print("  no zoom: the activity is spread over the screen, or there is none", file=sys.stderr)
        timeline = Timeline(speedups, info.duration)
        if timeline.sped:
            print(f"  {len(speedups)} still stretch{'es' if len(speedups) != 1 else ''} sped up: {info.duration:.1f} s becomes "
                  f"{timeline.out_duration():.1f} s", file=sys.stderr)

        if args.plan_out:
            if Path(args.plan_out).resolve() == src.resolve():
                raise ValueError("--plan-out would overwrite the input; choose another name")
            Path(args.plan_out).write_text(json.dumps(plan_to_json(zooms, info, str(src), wide, speedups), indent=2) + "\n", encoding="utf-8")
            print(f"  plan written to {args.plan_out}", file=sys.stderr)
            if not args.output:
                return 0

        out = Path(args.output) if args.output else _default_out(src)
        if out.resolve() == src.resolve():
            raise ValueError("the output would overwrite the input; choose another name with -o")
        crf, preset = {"draft": (24, "veryfast"), "good": (18, "medium"), "best": (15, "slow")}[args.quality]
        camera = Camera(zooms, info.width, info.height, info.duration, wide=wide)
        if out.suffix.lower() == ".gif":
            with tempfile.TemporaryDirectory() as tmp:
                mp4 = Path(tmp) / "polished.mp4"
                render(src, mp4, info, camera, style, args.width, _progress(info.duration), crf=crf, preset="veryfast", timeline=timeline)
                print("\n  making the GIF ...", file=sys.stderr)
                to_gif(mp4, out)
        else:
            render(src, out, info, camera, style, args.width, _progress(info.duration), crf=crf, preset=preset, timeline=timeline)
            print(file=sys.stderr)
        print(f"  wrote {out} ({os.path.getsize(out) / 2**20:.1f} MB)", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\n  stopped", file=sys.stderr)
        return 130
    except (MediaError, ValueError, OSError) as e:
        print(f"screen-polish: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
