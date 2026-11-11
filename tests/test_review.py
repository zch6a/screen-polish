"""Regression tests for the first independent review (POLISH_LOG, round 1). Each one failed before its fix."""

import json
import subprocess

import numpy as np
import pytest
import synth
from PIL import Image, ImageDraw, ImageFont

from screen_polish import cli
from screen_polish.analyze import Zoom, activity, plan
from screen_polish.camera import Camera
from screen_polish.media import ffmpeg_exe, parse_probe, probe


def run_ffmpeg(*args):
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", *args], check=True)


def test_audio_shorter_than_the_video_is_padded_not_cut(tmp_path):
    video = synth.write(tmp_path / "v.mp4", duration=8)
    mixed = tmp_path / "short_audio.mp4"
    run_ffmpeg("-i", str(video), "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-map", "0:v", "-map", "1:a", "-c:v", "copy",
               "-c:a", "aac", str(mixed))
    out = tmp_path / "out.mp4"
    assert cli.main([str(mixed), "-o", str(out), "--quality", "draft"]) == 0
    info = probe(out)
    assert abs(info.duration - 8.0) < 0.3 and info.has_audio


def test_the_length_is_the_videos_not_the_audios(tmp_path):
    video = synth.write(tmp_path / "v.mp4", duration=6)
    long_audio = tmp_path / "long_audio.mp4"
    run_ffmpeg("-i", str(video), "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-map", "0:v", "-map", "1:a", "-c:v", "copy",
               "-c:a", "aac", str(long_audio))
    assert abs(probe(long_audio).duration - 6.0) < 0.2
    webm = tmp_path / "streamed.webm"  # as a browser records it: no duration in the file
    proc = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(video), "-c:v", "libvpx", "-deadline", "realtime", "-b:v", "500k",
                           "-f", "webm", "-"], capture_output=True, check=True)
    webm.write_bytes(proc.stdout)
    assert abs(probe(webm).duration - 6.0) < 0.3


def test_variable_frame_rate_keeps_its_frames_and_29_97_stays():
    line = "  Stream #0:0: Video: h264 (High), yuv420p(progressive), 640x360, 14 kb/s, 6.40 fps, 30 tbr, 15360 tbn"
    assert parse_probe(line).fps == 30.0, "the timing rate, not the average of a recording that drops repeated frames"
    ntsc = "  Stream #0:0: Video: h264, yuv420p, 1920x1080, 29.97 fps, 30 tbr, 30k tbn"
    assert parse_probe(ntsc).fps == 29.97


def test_cover_art_is_not_a_video_and_tiny_videos_are():
    mp3 = "  Stream #0:0: Audio: mp3, 44100 Hz\n  Stream #0:1: Video: png, rgb24, 300x300 [SAR 1:1 DAR 1:1], 90k tbr (attached pic)"
    with pytest.raises(Exception, match="no video stream"):
        parse_probe(mp3)
    assert (parse_probe("  Stream #0:0: Video: h264, yuv420p, 8x8, 15 fps").width) == 8


def test_normal_size_text_on_a_1080p_screen_is_found(tmp_path):
    font = ImageFont.load_default(size=14)
    text = "def polish(video): return video.zoomed()"

    def typing(t):
        img = Image.new("RGB", (1920, 1080), (236, 236, 236))
        d = ImageDraw.Draw(img)
        d.rectangle((700, 120, 1400, 160), fill=(255, 255, 255))
        d.text((710, 130), text[: int(max(0.0, t - 2) * 8)], fill=(30, 30, 30), font=font)
        return np.asarray(img)

    path = synth.write(tmp_path / "type1080.mp4", typing, duration=7, fps=10, size=(1920, 1080))
    zooms = plan(activity(path, probe(path)))
    assert len(zooms) == 1 and zooms[0].x <= 710 and zooms[0].y <= 130 <= zooms[0].y + zooms[0].h


def test_a_mouse_that_rests_and_moves_on_starts_no_zoom(tmp_path):
    points = [(300, 200), (520, 330), (380, 420), (600, 250), (450, 180), (320, 380), (560, 420)]
    base = synth.desktop(1)

    def mouse(t, period=1.6, move=0.5):
        k = int(t // period) % (len(points) - 1)
        f = min(1.0, (t % period) / move)
        (x0, y0), (x1, y1) = points[k], points[k + 1]
        return synth.cursor(base.copy(), (x0 + (x1 - x0) * f) * 0.5 + 60, (y0 + (y1 - y0) * f) * 0.5 + 40)

    path = synth.write(tmp_path / "rest.mp4", mouse, duration=10)
    assert plan(activity(path, probe(path))) == []


def test_typing_in_the_first_seconds_is_found(tmp_path):
    def early(t):
        return synth.frame_at(t + 1.8) if t < 2.0 else synth.frame_at(3.8)  # the typing of 2.0-3.8 s, at 0.2-2.0 s

    path = synth.write(tmp_path / "early.mp4", early, duration=6)
    zooms = plan(activity(path, probe(path)))
    assert len(zooms) == 1 and zooms[0].start < 1.0


def test_the_whole_screen_shows_when_it_changes_before_the_first_zoom_and_after_the_last():
    before = Camera([Zoom(7.0, 9.5, 0, 180, 320, 180)], 640, 360, 11.0, wide=[6.0])
    assert before.rect(6.3) == pytest.approx((0, 0, 640, 360)), "the new page, before zooming in"
    after = Camera([Zoom(2.0, 5.5, 320, 0, 320, 180)], 640, 360, 11.0, wide=[6.0])
    # The activity runs to 5.5 s and a move takes 0.8 s: out as soon as it ends, whole by 6.3 s (it used to stay in).
    assert after.rect(6.3) == pytest.approx((0, 0, 640, 360)), "out as soon as the activity allows"


def test_a_plans_rectangle_is_shown_whole_whatever_its_shape():
    cam = Camera([Zoom(1.0, 4.0, 100, 30, 200, 300)], 640, 360, 6.0)
    x0, y0, x1, y1 = cam.rect(2.5)
    assert x0 <= 100 and y0 <= 30 and x1 >= 300 and y1 >= 330


@pytest.mark.parametrize("plan_data, message", [
    ([], "JSON object"),
    ({"version": 1, "source": [640, 360], "zooms": []}, "source"),
    ({"version": 1, "source": {"width": 0, "height": 360}, "zooms": []}, "more than 0"),
    ({"version": 1, "source": {"width": "640", "height": 360}, "zooms": []}, "number"),
    ({"version": 1, "zooms": [{"start": float("nan"), "end": 2, "x": 0, "y": 0, "w": 9, "h": 9}]}, "number"),
    ({"version": 1, "zooms": [5]}, "object"),
    ({"version": 1, "zooms": [], "wide": 3}, "list"),
])
def test_bad_plans_are_said_plainly(plan_data, message):
    info = type("I", (), {"width": 640, "height": 360})()
    with pytest.raises(ValueError, match=message):
        cli.plan_from_json(plan_data, info)


def test_bad_click_logs_and_options_are_said_plainly(tmp_path, capsys):
    video = synth.write(tmp_path / "v.mp4", duration=3)
    for content in ('{"clicks": [{"t": 1, "x": 2}]}', '{"clicks": 5}', "[1, 2]", '[{"t": 1, "x": "2", "y": 3}]'):
        log = tmp_path / "clicks.json"
        log.write_text(content, encoding="utf-8")
        assert cli.main([str(video), "--clicks", str(log), "--plan-out", str(tmp_path / "p.json")]) == 1
    for bad in (["--padding", "60"], ["--width", "-50"], ["--radius", "80"], ["--max-zoom", "0"]):
        assert cli.main([str(video), *bad]) == 1
    size = video.stat().st_size
    assert cli.main([str(video), "--plan-out", str(video)]) == 1 and video.stat().st_size == size, "the input is never overwritten"


def test_a_failed_render_leaves_no_file_and_keeps_an_earlier_one(tmp_path, monkeypatch):
    video = synth.write(tmp_path / "v.mp4", duration=3)
    out = tmp_path / "out.mp4"
    out.write_bytes(b"an earlier good result")

    def stop(t):
        if t > 0.5:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_progress", lambda duration: stop)
    assert cli.main([str(video), "-o", str(out)]) == 130
    assert out.read_bytes() == b"an earlier good result" and not list(tmp_path.glob(".*partial*"))


def test_the_output_says_its_colours_are_bt709(tmp_path):
    video = synth.write(tmp_path / "v.mp4", duration=2)
    out = tmp_path / "out.mp4"
    assert cli.main([str(video), "-o", str(out), "--no-zoom", "--quality", "draft"]) == 0
    banner = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "bt709" in banner


def test_plans_written_and_read_keep_everything(tmp_path):
    info = type("I", (), {"width": 640, "height": 360, "duration": 11.0})()
    data = cli.plan_to_json([Zoom(1.0, 3.0, 10, 20, 320, 180)], info, "demo.mp4", wide=[6.0])
    zooms, wide = cli.plan_from_json(json.loads(json.dumps(data)), info)
    assert (zooms[0].x, zooms[0].h, wide) == (10, 180, [6.0])
