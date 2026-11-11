import json

import numpy as np
import pytest
import synth

from screen_polish import cli
from screen_polish.media import frames, probe
from screen_polish.render import Style, layout


@pytest.fixture(scope="session")
def demo(tmp_path_factory):
    return synth.write(tmp_path_factory.mktemp("v") / "demo.mp4", audio=True)


def frame_at(path, t):
    info = probe(path)
    for i, f in enumerate(frames(path, info.width, info.height, info.fps)):
        if i == round(t * info.fps):
            return f, info
    raise AssertionError(f"no frame at {t} s")


def dark_pixels(f, lay):
    k = 12  # inset past the rounded corners, where the dark background shows
    inner = f[lay.top + k:lay.top + lay.inner_h - k, lay.left + k:lay.left + lay.inner_w - k]
    return int((inner.max(axis=2) < 50).sum())  # the typed marks are the only near-black things


@pytest.fixture(scope="session")
def polished(demo, tmp_path_factory):
    out = tmp_path_factory.mktemp("o") / "demo.polished.mp4"
    assert cli.main([str(demo), "-o", str(out), "--quality", "draft"]) == 0
    return out


def test_the_result_is_zoomed_framed_and_keeps_the_sound(demo, polished, tmp_path):
    info = probe(polished)
    assert (info.width, info.height) == (synth.W, synth.H) and abs(info.duration - synth.DURATION) < 0.3 and info.has_audio
    lay = layout(probe(demo), Style())
    zoomed, _ = frame_at(polished, 3.8)
    flat_out = tmp_path / "flat.mp4"
    assert cli.main([str(demo), "-o", str(flat_out), "--no-zoom", "--quality", "draft"]) == 0
    flat, _ = frame_at(flat_out, 3.8)
    ratio = dark_pixels(zoomed, lay) / dark_pixels(flat, lay)
    assert 3.0 < ratio < 5.0, f"a 2x zoom shows the marks 4x as large, not {ratio:.2f}x"
    corner = zoomed[2, 2].astype(int)
    assert np.abs(corner - [15, 23, 42]).max() < 12, f"the midnight background in the corner, not {corner}"
    still, _ = frame_at(polished, 0.5)
    assert dark_pixels(still, lay) < dark_pixels(zoomed, lay) / 50 and abs(int(still[lay.top + lay.inner_h // 2, lay.left + 5].mean()) - 236) < 30, \
        "before the activity: the whole screen, inside the frame"


def test_a_gif(demo, tmp_path):
    out = tmp_path / "demo.gif"
    assert cli.main([str(demo), "-o", str(out), "--width", "320"]) == 0
    assert out.read_bytes()[:6] == b"GIF89a" and probe(out).width == 320


def test_a_plan_can_be_written_edited_and_used(demo, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    assert cli.main([str(demo), "--plan-out", str(plan)]) == 0
    assert not (tmp_path / "demo.polished.mp4").exists(), "--plan-out alone renders nothing"
    data = json.loads(plan.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["zooms"]) == 2 and data["source"]["width"] == synth.W and data["wide"] == [6.0]
    data["zooms"] = data["zooms"][:1]
    plan.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "one.mp4"
    assert cli.main([str(demo), "--plan", str(plan), "-o", str(out), "--quality", "draft"]) == 0
    assert "zoom    2.0" in capsys.readouterr().err
    data["zooms"][0]["end"] = 0
    plan.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main([str(demo), "--plan", str(plan), "-o", str(out)]) == 1


def test_plans_scale_to_another_size():
    info = type("I", (), {"width": 1280, "height": 720})()
    zooms, wide = cli.plan_from_json({"version": 1, "source": {"width": 640, "height": 360},
                                "zooms": [{"start": 1, "end": 2, "x": 320, "y": 0, "w": 320, "h": 180}]}, info)
    assert (zooms[0].x, zooms[0].w, zooms[0].reason) == (640, 640, "manual")
    assert wide == []


def test_mistakes_are_said_plainly(demo, tmp_path, capsys):
    assert cli.main([str(tmp_path / "missing.mp4")]) == 1 and "no such file" in capsys.readouterr().err
    assert cli.main([str(demo), "-o", str(demo)]) == 1 and "overwrite the input" in capsys.readouterr().err
    assert cli.main([str(demo), "--background", "pink-ish"]) == 1 and "not a colour" in capsys.readouterr().err
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    assert cli.main([str(text)]) == 1 and "no video stream" in capsys.readouterr().err


def test_layouts_are_even_and_centred():
    info = type("I", (), {"width": 1366, "height": 768})()
    lay = layout(info, Style())
    assert lay.out_w % 2 == lay.out_h % 2 == lay.inner_w % 2 == lay.inner_h % 2 == 0
    assert abs((lay.out_w - lay.inner_w) / 2 - lay.left) <= 1 and lay.inner_w < lay.out_w
    tall = layout(type("I", (), {"width": 1080, "height": 1920})(), Style())
    assert tall.top >= int(0.06 * tall.out_w) - 2 and tall.inner_h <= tall.out_h
    edge = layout(info, Style(frame=False))
    assert (edge.inner_w, edge.left) == (edge.out_w, 0)
