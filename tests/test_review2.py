"""Regression tests for the second independent review (POLISH_LOG, round 3)."""

import json
import subprocess

import numpy as np
import pytest
import synth

from screen_polish import cli
from screen_polish.analyze import activity, plan
from screen_polish.camera import Camera
from screen_polish.media import ffmpeg_exe, frames, probe
from screen_polish.render import Style, layout
from screen_polish.timeline import Speedup, Timeline, idle_stretches


def encode(path, make, duration, fps, extra=()):
    w, h = synth.W, synth.H
    cmd = [ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *extra, "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(int(duration * fps)):
        proc.stdin.write(make(i / fps).tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return path


def test_encoder_noise_at_30_fps_is_not_activity(tmp_path):
    path = encode(tmp_path / "demo30.mp4", synth.frame_at, synth.DURATION, 30)
    zooms = plan(activity(path, probe(path)))
    assert len(zooms) == 2 and abs(zooms[0].start - 2.0) < 0.3, "the same zooms as at 15 fps"


def test_keyframes_do_not_break_a_still_stretch(tmp_path):
    still = encode(tmp_path / "still.mp4", lambda t: synth.frame_at(min(t, 5.0)), 20, 30, ("-g", "250"))
    assert len(idle_stretches(activity(still, probe(still)))) == 1


def test_typing_in_the_last_seconds_gets_a_zoom(tmp_path):
    def late(t):  # the demo's typing, 7.1-9.0 s of a 9 s video
        return synth.frame_at(t - 5.1) if t >= 7.1 else synth.desktop(1)

    path = synth.write(tmp_path / "late.mp4", late, duration=9)
    zooms = plan(activity(path, probe(path)))
    assert len(zooms) == 1 and zooms[0].start >= 7.0


def test_a_pointer_circling_one_spot_is_not_still(tmp_path):
    base = synth.desktop(1)

    def circling(t):
        if t < 2:
            return base
        return synth.cursor(base.copy(), 400 + 25 * np.cos(t * 6.283), 200 + 25 * np.sin(t * 6.283))

    path = synth.write(tmp_path / "circle.mp4", circling, duration=8)
    assert all(s.end <= 2.5 for s in idle_stretches(activity(path, probe(path))))


def test_sound_and_picture_stay_together_across_a_speed_up(tmp_path):
    """A flash and a beep at the same moments, 1 s and 9 s, with a still stretch sped up in between."""
    base = synth.desktop(1)

    def flashes(t):
        return np.full_like(base, 255) if (1.0 <= t < 1.2 or 9.0 <= t < 9.2) else base

    video = synth.write(tmp_path / "v.mp4", flashes, duration=11)
    av = tmp_path / "av.mp4"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(video), "-f", "lavfi", "-i", "sine=frequency=1000:duration=11",
                    "-af", "volume=0:enable='not(between(t,1,1.2)+between(t,9,9.2))'", "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", str(av)], check=True)
    out = tmp_path / "out.mp4"
    assert cli.main([str(av), "-o", str(out), "--no-zoom", "--no-frame", "--speed-idle", "4", "--quality", "draft"]) == 0
    info = probe(out)
    assert info.duration < 8, "the still stretch is sped up"
    bright = [i / info.fps for i, f in enumerate(frames(out, 64, 36, info.fps, gray=True)) if f.mean() > 245]
    pcm = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(out), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
                         capture_output=True, check=True).stdout
    level = np.abs(np.frombuffer(pcm, np.int16).astype(float))
    loud = np.nonzero(level > 3000)[0] / 8000
    onsets = [loud[0]] + [b for a, b in zip(loud, loud[1:], strict=False) if b - a > 0.5]
    flash_onsets = [bright[0]] + [b for a, b in zip(bright, bright[1:], strict=False) if b - a > 0.5]
    assert len(onsets) == len(flash_onsets) == 2
    for beep, flash in zip(onsets, flash_onsets, strict=True):
        assert abs(beep - flash) < 0.08, f"beep at {beep:.3f} s, flash at {flash:.3f} s"


def test_speed_ups_are_snapped_to_frames():
    tl = Timeline([Speedup(1.03, 5.11, 4.0)], 10.0, fps=15)
    sped = next(p for p in tl.parts if p.factor == 4.0)
    assert abs(sped.start * 15 - round(sped.start * 15)) < 1e-9
    assert abs((sped.end - sped.start) * 15 / 4 - round((sped.end - sped.start) * 15 / 4)) < 1e-9


def test_camera_moves_stay_at_normal_speed(tmp_path):
    src = synth.write(tmp_path / "long.mp4", duration=16)
    plan_file = tmp_path / "plan.json"
    assert cli.main([str(src), "--speed-idle", "4", "--plan-out", str(plan_file)]) == 0
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    zooms, wide = cli.plan_from_json(data, probe(src))
    cam = Camera(zooms, synth.W, synth.H, 16.0, wide=wide)
    for at, _ in cam.shots[1:]:
        for s in data["speed"]:
            assert at + cam.t <= s["start"] + 1e-3 or at >= s["end"] - 1e-3, f"move at {at:.2f} s inside the speed-up {s}"


def test_a_plan_with_speed_idle_or_clicks(tmp_path, capsys):
    src = synth.write(tmp_path / "long.mp4", duration=16)
    plan_file = tmp_path / "plan.json"
    assert cli.main([str(src), "--plan-out", str(plan_file)]) == 0
    out = tmp_path / "out.mp4"
    assert cli.main([str(src), "--plan", str(plan_file), "--speed-idle", "4", "-o", str(out), "--quality", "draft"]) == 0
    assert probe(out).duration < 14, "--speed-idle is applied to a plan without speed-ups"
    clicks = tmp_path / "clicks.json"
    clicks.write_text('[{"t": 1, "x": 10, "y": 10}]', encoding="utf-8")
    assert cli.main([str(src), "--plan", str(plan_file), "--clicks", str(clicks)]) == 1 and "--clicks" in capsys.readouterr().err


def test_padding_means_the_same_with_and_without_aspect():
    info = type("I", (), {"width": 1920, "height": 1080})()
    plain, shaped = layout(info, Style()), layout(info, Style(aspect=16 / 9))
    assert (plain.inner_w, plain.inner_h) == (shaped.inner_w, shaped.inner_h)


def test_a_shape_a_pixel_off_gets_no_hairline_bars():
    cam = Camera([], 1081, 1920, 5.0, aspect=9 / 16)
    assert cam.rect(1.0) == pytest.approx((0, 0, 1081, 1920))


def test_the_printed_zoom_factor_is_what_the_camera_shows(tmp_path, capsys):
    src = synth.write(tmp_path / "v.mp4", duration=4)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"version": 1, "zooms": [{"start": 1, "end": 3, "x": 100, "y": 20, "w": 60, "h": 300}]}),
                         encoding="utf-8")
    assert cli.main([str(src), "--plan", str(plan_file), "--plan-out", str(tmp_path / "p2.json")]) == 0
    # 60x300 in a 16:9 view is 533 px wide: 640/533 = 1.2x, not 640/60 = 10.7x
    assert "1.2x" in capsys.readouterr().err
