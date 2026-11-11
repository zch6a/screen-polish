"""Vertical (and other) output shapes (round 3)."""

import numpy as np
import pytest
import synth

from screen_polish import cli
from screen_polish.camera import Camera
from screen_polish.media import frames, probe
from screen_polish.render import BARS, Style, layout


def info(w, h):
    return type("I", (), {"width": w, "height": h})()


def test_shapes_are_parsed_and_checked():
    assert cli.parse_aspect("9:16") == pytest.approx(0.5625) and cli.parse_aspect("1:1") == 1.0 and cli.parse_aspect("source") is None
    for bad in ("tall", "0:5", "1:20"):
        with pytest.raises(ValueError):
            cli.parse_aspect(bad)


def test_a_vertical_layout_keeps_the_long_side():
    lay = layout(info(1920, 1080), Style(aspect=9 / 16))
    assert (lay.out_w, lay.out_h) == (1080, 1920)
    assert abs(lay.inner_w / lay.inner_h - 9 / 16) < 0.01 and lay.left > 0 and lay.top > 0
    square = layout(info(1920, 1080), Style(aspect=1.0))
    assert square.out_w == square.out_h == 1920
    assert layout(info(1920, 1080), Style(aspect=9 / 16), out_width=720).out_h == 1280


def test_a_vertical_camera_shows_the_whole_screen_with_room_above_and_below():
    cam = Camera([], 640, 360, 5.0, aspect=9 / 16)
    x0, y0, x1, y1 = cam.rect(1.0)
    assert (x0, x1) == pytest.approx((0, 640)) and y0 < 0 and y1 > 360, "the whole width, taller than the screen"


def test_a_vertical_video_is_framed_with_bars_and_zooms(tmp_path):
    src = synth.write(tmp_path / "demo.mp4")
    out = tmp_path / "vertical.mp4"
    assert cli.main([str(src), "-o", str(out), "--aspect", "9:16", "--quality", "draft"]) == 0
    v = probe(out)
    assert (v.width, v.height) == (360, 640) and abs(v.duration - synth.DURATION) < 0.3
    lay = layout(probe(src), Style(aspect=9 / 16))
    shots = {}
    for i, f in enumerate(frames(out, v.width, v.height, v.fps)):
        if i in (round(0.5 * v.fps), round(3.8 * v.fps)):
            shots[i] = f
    still, zoomed = shots[round(0.5 * v.fps)], shots[round(3.8 * v.fps)]
    bar = still[lay.top + 20, lay.left + lay.inner_w // 2].astype(int)
    assert np.abs(bar - BARS).max() < 12, f"a dark bar above the screen, not {bar}"
    middle = still[lay.top + lay.inner_h // 2, lay.left + 20].astype(int)
    assert middle.mean() > 150, "the screen itself in the middle"
    def bars(f):  # share of the frame's inside that is bar
        inner = f[lay.top + 12:lay.top + lay.inner_h - 12, lay.left + 12:lay.left + lay.inner_w - 12].astype(int)
        return float((np.abs(inner - BARS).max(axis=2) < 10).mean())

    # The whole 16:9 screen in a 9:16 frame leaves about two thirds bars; a 2x zoom shows it twice as tall.
    assert bars(still) > 0.55 and bars(zoomed) < 0.45, (bars(still), bars(zoomed))
