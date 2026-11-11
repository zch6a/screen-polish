"""Recordings that fooled the first version (POLISH_LOG, round 1): a blinking caret and a wandering mouse
made zooms, a portrait phone video got a zoom that cut its field in half and zooms that overlapped."""

import pytest
import synth

from screen_polish.analyze import activity, plan
from screen_polish.camera import Camera
from screen_polish.media import probe


def zooms_of(path):
    return plan(activity(path, probe(path)))


def test_a_blinking_caret_is_not_activity(tmp_path):
    assert zooms_of(synth.write(tmp_path / "caret.mp4", synth.caret, duration=8)) == []


def test_a_wandering_mouse_is_not_activity(tmp_path):
    assert zooms_of(synth.write(tmp_path / "mouse.mp4", synth.wandering_mouse, duration=8)) == []


def test_a_dark_theme_is_read_like_a_light_one(tmp_path):
    zooms = zooms_of(synth.write(tmp_path / "dark.mp4", synth.dark))
    assert [(round(z.start), round(z.end)) for z in zooms] == [(2, 5), (7, 10)]


def test_typing_along_a_wide_field_on_a_phone_is_followed_not_cut(tmp_path):
    path = synth.write(tmp_path / "phone.mp4", synth.phone, duration=8, size=synth.PHONE)
    info = probe(path)
    assert (info.width, info.height) == synth.PHONE
    zooms = zooms_of(path)
    assert zooms, "the typing is found"
    for a, b in zip(zooms, zooms[1:], strict=False):
        assert b.start >= a.end, "one after the other, never two at once"
    for z in zooms:
        assert synth.PHONE[0] / z.w >= 1.25, "each is a real zoom"
    x0, y0, x1, y1 = synth.PHONE_FIELD
    assert min(z.y for z in zooms) <= y0 and max(z.y + z.h for z in zooms) >= y1
    assert min(z.x for z in zooms) <= x0 + 40 and max(z.x + z.w for z in zooms) >= x1 - 40, "the camera covers the field as typing moves"
    cam = Camera(zooms, info.width, info.height, info.duration)
    rects = [cam.rect(t / 15) for t in range(int(info.duration * 15))]
    step = max(max(abs(a - b) for a, b in zip(r, s, strict=True)) for r, s in zip(rects, rects[1:], strict=False))
    assert step < 0.1 * info.height, "no jump: under a tenth of the frame per frame"


@pytest.mark.slow
def test_small_text_on_a_4k_screen_is_found(tmp_path):
    path = synth.write(tmp_path / "uhd.mp4", synth.uhd_small_text, duration=6, fps=10, size=synth.UHD)
    zooms = zooms_of(path)
    assert len(zooms) == 1
    z = zooms[0]
    x0, y0, x1, y1 = synth.UHD_FIELD
    assert 1.4 <= z.start <= 2.2 and z.x <= x0 + 200 and z.y <= y0 and z.y + z.h >= y1
