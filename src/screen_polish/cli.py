"""screen-polish: make a screen recording you already have look produced.

    screen-polish demo.mp4                       zoom where things happen, frame it, write demo.polished.mp4
    screen-polish demo.mp4 -o demo.gif           the same as a GIF
    screen-polish demo.mp4 --plan-out plan.json  only work out the zooms, to look at or edit
    screen-polish demo.mp4 --plan plan.json      use those zooms
"""

from __future__ import annotations

import argparse
import json
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

PLAN_VERSION = 1


def plan_to_json(zooms: list[Zoom], info, source: str, wide: list[float] | None = None) -> dict:
    """`wide`: the moments the whole screen changed, when the camera shows all of it."""
    return {"version": PLAN_VERSION, "source": {"file": Path(source).name, "width": info.width, "height": info.height,
                                                "duration": round(info.duration, 3)},
            "zooms": [z.to_dict() for z in zooms], "wide": list(wide or [])}


def plan_from_json(data: dict, info) -> tuple[list[Zoom], list[float]]:
    """Zooms (and wide moments) from a plan file; a plan made for another size is scaled to this one."""
    if data.get("version") != PLAN_VERSION:
        raise ValueError(f"plan version {data.get('version')!r} is not {PLAN_VERSION}")
    src = data.get("source") or {}
    sx = info.width / src.get("width", info.width)
    sy = info.height / src.get("height", info.height)
    zooms = []
    for i, z in enumerate(data.get("zooms") or []):
        try:
            zooms.append(Zoom(float(z["start"]), float(z["end"]), float(z["x"]) * sx, float(z["y"]) * sy, float(z["w"]) * sx,
                              float(z["h"]) * sy, str(z.get("reason", "manual"))))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"zoom {i + 1} in the plan: needs start, end, x, y, w, h numbers ({e})") from e
        if zooms[-1].end <= zooms[-1].start or zooms[-1].w <= 0 or zooms[-1].h <= 0:
            raise ValueError(f"zoom {i + 1} in the plan: end must be after start, and w and h positive")
    try:
        wide = sorted(float(w) for w in data.get("wide") or [])
    except (TypeError, ValueError) as e:
        raise ValueError(f"\"wide\" in the plan must be a list of times in seconds ({e})") from e
    return sorted(zooms, key=lambda z: z.start), wide


def load_clicks(path: str) -> list[dict]:
    """A click log: {"clicks": [{"t": seconds, "x": px, "y": px}, ...]} or just the list."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    clicks = data.get("clicks", []) if isinstance(data, dict) else data
    return [{"t": float(c["t"]), "x": float(c["x"]), "y": float(c["y"])} for c in clicks]


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
        style = Style(background=args.background, padding=max(0.0, args.padding) / 100, radius=max(0.0, args.radius) / 100,
                      shadow=not args.no_shadow, frame=not args.no_frame)
        if style.background not in BACKGROUNDS:
            from .render import _color

            _color(style.background)  # a bad colour is said now, not after the analysis
        print(f"{src.name}: {info.width}x{info.height}, {info.fps:g} fps, {info.duration:.1f} s"
              f"{', with audio' if info.has_audio else ''}", file=sys.stderr)

        wide: list[float] = []
        if args.no_zoom:
            zooms: list[Zoom] = []
        elif args.plan:
            zooms, wide = plan_from_json(json.loads(Path(args.plan).read_text(encoding="utf-8")), info)
        else:
            print("  finding where things happen ...", file=sys.stderr)
            act = activity(src, info)
            wide = wide_moments(act)
            if args.clicks:
                add_clicks(act, load_clicks(args.clicks))
            zooms = plan(act, max_zoom=max(1.0, args.max_zoom))
        for z in zooms:
            print(f"  zoom {z.start:6.1f}-{z.end:6.1f} s  {info.width / z.w:.1f}x on ({z.x:.0f}, {z.y:.0f}) {z.w:.0f}x{z.h:.0f}", file=sys.stderr)
        if not zooms and not args.no_zoom:
            print("  no zoom: the activity is spread over the screen, or there is none", file=sys.stderr)

        if args.plan_out:
            Path(args.plan_out).write_text(json.dumps(plan_to_json(zooms, info, str(src), wide), indent=2) + "\n", encoding="utf-8")
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
                render(src, mp4, info, camera, style, args.width, _progress(info.duration), crf=crf, preset="veryfast")
                print("\n  making the GIF ...", file=sys.stderr)
                to_gif(mp4, out)
        else:
            render(src, out, info, camera, style, args.width, _progress(info.duration), crf=crf, preset=preset)
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
