import numpy as np
import pytest
import synth

from screen_polish.analyze import Zoom, activity, add_clicks, plan, wide_moments
from screen_polish.camera import Camera
from screen_polish.media import parse_probe, probe


@pytest.fixture(scope="session")
def demo(tmp_path_factory):
    return synth.write(tmp_path_factory.mktemp("v") / "demo.mp4")


def inside(box, zoom: Zoom, slack=0) -> bool:
    x0, y0, x1, y1 = box
    return zoom.x - slack <= x0 and zoom.y - slack <= y0 and x1 <= zoom.x + zoom.w + slack and y1 <= zoom.y + zoom.h + slack


def test_probe_reads_the_banner():
    text = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':
  Duration: 00:01:02.50, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), 1920x1080 [SAR 1:1 DAR 16:9], 1100 kb/s, 29.97 fps, 30 tbr
  Stream #0:1[0x2](und): Audio: aac (LC), 48000 Hz, stereo, fltp, 128 kb/s"""
    info = parse_probe(text)
    assert (info.width, info.height, info.fps, info.duration, info.has_audio) == (1920, 1080, 29.97, 62.5, True)
    phone = text.replace("Stream #0:1", "    Side data:\n      displaymatrix: rotation of -90.00 degrees\n  Stream #0:9").replace("Audio", "Data")
    rotated = parse_probe(phone)
    assert (rotated.width, rotated.height, rotated.has_audio) == (1080, 1920, False), "portrait as decoded"


def test_the_zooms_follow_the_activity(demo):
    info = probe(demo)
    assert (info.width, info.height) == (synth.W, synth.H) and abs(info.duration - synth.DURATION) < 0.2
    zooms = plan(activity(demo, info))
    assert len(zooms) == 2, zooms
    typing, menu = zooms
    assert 1.8 <= typing.start <= 2.5 and 4.5 <= typing.end < 6.0, typing
    assert inside(synth.TYPING, typing, slack=4), typing
    assert 6.8 <= menu.start <= 7.5 and 9.0 <= menu.end <= 9.8, menu
    assert inside(synth.MENU, menu, slack=4), menu
    for z in zooms:
        assert abs(z.w / z.h - synth.W / synth.H) < 0.01, "the video's aspect ratio"
        assert synth.W / z.w <= 2.0 + 1e-6, "no closer than --max-zoom"
        assert not (z.start < 6.0 < z.end), "no zoom across the page change"


def test_a_still_video_and_a_scrolling_one_get_no_zoom(tmp_path):
    still = synth.write(tmp_path / "still.mp4", lambda t: synth.desktop(1), duration=4)
    assert plan(activity(still, probe(still))) == []

    def scrolling(t):  # the whole page moves up all the time
        return np.roll(synth.desktop(1), -int(t * 40), axis=0)

    scroll = synth.write(tmp_path / "scroll.mp4", scrolling, duration=4)
    assert plan(activity(scroll, probe(scroll))) == []


def test_clicks_alone_make_a_zoom(tmp_path):
    still = synth.write(tmp_path / "still.mp4", lambda t: synth.desktop(1), duration=5)
    act = activity(still, probe(still))
    add_clicks(act, [{"t": 1.0, "x": 500, "y": 300}, {"t": 1.6, "x": 510, "y": 300}, {"t": 2.2, "x": 505, "y": 305}])
    zooms = plan(act)
    assert len(zooms) == 1 and zooms[0].x <= 500 <= zooms[0].x + zooms[0].w and zooms[0].y <= 300 <= zooms[0].y + zooms[0].h


def test_the_camera_moves_smoothly_and_arrives_before_the_activity():
    zooms = [Zoom(3.0, 6.0, 320, 0, 320, 180), Zoom(6.8, 8.0, 0, 180, 320, 180)]
    cam = Camera(zooms, 640, 360, 12.0)
    assert cam.rect(0.5) == (0, 0, 640, 360)
    assert cam.rect(3.0) == pytest.approx((320, 0, 640, 180)), "arrived before the activity"
    assert cam.rect(11.9) == pytest.approx((0, 0, 640, 360)), "back to the whole screen at the end"
    # close in time: a pan from one to the other, never back to the whole screen in between
    assert max(cam.rect(t / 30)[2] - cam.rect(t / 30)[0] for t in range(int(5.5 * 30), int(7.5 * 30))) < 640
    rects = [cam.rect(t / 30) for t in range(0, 12 * 30)]
    jumps = [max(abs(a - b) for a, b in zip(r, s, strict=True)) for r, s in zip(rects, rects[1:], strict=False)]
    assert max(jumps) < 40, "no jump between frames"


def test_the_camera_shows_the_whole_screen_when_it_changes(demo):
    info = probe(demo)
    act = activity(demo, info)
    assert wide_moments(act) == [6.0]
    cam = Camera(plan(act), info.width, info.height, info.duration, wide=wide_moments(act))
    assert cam.rect(6.0) == pytest.approx((0, 0, synth.W, synth.H)), "all of the new page as it appears"
    assert cam.rect(8.0)[2] - cam.rect(8.0)[0] < synth.W, "then in to the menu"
    straight = Camera(plan(act), info.width, info.height, info.duration)
    assert straight.rect(6.0)[2] - straight.rect(6.0)[0] < synth.W, "without it, the camera would pan across the change"
