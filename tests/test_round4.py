"""Round 4 (POLISH_LOG): what reading the whole thing again turned up."""

import synth

from screen_polish import cli
from screen_polish.analyze import Zoom
from screen_polish.camera import Camera
from screen_polish.timeline import Speedup, Timeline


def test_outputs_that_are_not_mp4_or_gif_are_refused(tmp_path, capsys):
    src = synth.write(tmp_path / "v.mp4", duration=2)
    for name in ("out.webm", "out.mkv", "out.avi", "out"):
        assert cli.main([str(src), "-o", str(tmp_path / name)]) == 1
        assert "MP4" in capsys.readouterr().err
        assert not (tmp_path / name).exists()
    assert cli.main([str(src), "-o", str(tmp_path / "missing" / "out.mp4")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_a_bad_option_is_said_before_the_video_is_read(tmp_path, capsys):
    assert cli.main([str(tmp_path / "not_there.mp4"), "--padding", "90"]) == 1
    err = capsys.readouterr().err
    assert "--padding" in err and "no such file" not in err


def test_many_shots_and_parts_are_looked_up_not_scanned():
    zooms = [Zoom(5.0 * k + 1, 5.0 * k + 3, 0, 0, 320, 180) for k in range(400)]  # a long video with 400 zooms
    cam = Camera(zooms, 640, 360, 2000.0)
    assert len(cam.shots) > 400
    assert cam.rect(5.0 * 200 + 2) == (0, 0, 320, 180)
    tl = Timeline([Speedup(5.0 * k + 3.5, 5.0 * k + 4.8, 4.0) for k in range(400)], 2000.0)
    assert tl.speed_at(5.0 * 300 + 4.0) == 4.0 and tl.speed_at(5.0 * 300 + 2.0) == 1.0 and tl.speed_at(1999.9) == 1.0
