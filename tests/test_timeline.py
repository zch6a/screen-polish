"""Speeding up the still stretches (round 2)."""

import json
import re
import subprocess

import pytest
import synth

from screen_polish import cli
from screen_polish.analyze import activity
from screen_polish.media import ffmpeg_exe, probe
from screen_polish.timeline import Speedup, Timeline, idle_stretches


@pytest.fixture(scope="module")
def long_demo(tmp_path_factory):
    """The demo script, then still until 16 s: 6.5 still seconds at the end."""
    return synth.write(tmp_path_factory.mktemp("v") / "long.mp4", duration=16, audio=True)


def test_the_timeline_maps_source_to_output():
    tl = Timeline([Speedup(2.0, 6.0, 4.0)], 10.0)
    assert [(p.start, p.end, p.factor) for p in tl.parts] == [(0.0, 2.0, 1.0), (2.0, 6.0, 4.0), (6.0, 10.0, 1.0)]
    assert tl.out_duration() == pytest.approx(2 + 4 / 4 + 4) and tl.speed_at(3.0) == 4.0 and tl.speed_at(7.0) == 1.0
    graph = tl.audio_graph()
    assert graph.startswith("[1:a:0]asplit=3") and "atempo=2,atempo=2" in graph and graph.endswith("concat=n=3:v=0:a=1,apad[aout]")
    assert not Timeline([], 10.0).sped
    overlapping = Timeline([Speedup(2, 6, 4), Speedup(5, 8, 2), Speedup(9, 12, 3)], 10.0)
    assert [(p.start, p.end) for p in overlapping.parts] == [(0, 2), (2, 6), (6, 8), (8, 9), (9, 10)], "clipped, never overlapping"


def test_only_long_still_stretches_are_sped_up(long_demo, tmp_path):
    zooms = idle_stretches(activity(long_demo, probe(long_demo)), factor=4.0)
    assert len(zooms) == 1 and 9.8 <= zooms[0].start <= 10.3 and 15.2 <= zooms[0].end <= 15.6, zooms
    demo = synth.write(tmp_path / "demo.mp4")
    assert idle_stretches(activity(demo, probe(demo))) == [], "its still parts are all under two seconds"
    caret = synth.write(tmp_path / "caret.mp4", synth.caret, duration=8)
    assert len(idle_stretches(activity(caret, probe(caret)))) == 1, "a blinking caret is still"


def test_a_sped_up_video_keeps_its_sound_in_step(long_demo, tmp_path):
    out = tmp_path / "fast.mp4"
    plan = tmp_path / "plan.json"
    assert cli.main([str(long_demo), "-o", str(out), "--speed-idle", "4", "--quality", "draft", "--plan-out", str(plan)]) == 0
    info = probe(out)
    assert info.has_audio and abs(info.duration - (16 - 5.25 * 0.75)) < 0.3
    banner = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", banner).groups()
    assert abs(int(h) * 3600 + int(m) * 60 + float(s) - info.duration) < 0.3, "the sound does not run on past the video"
    data = json.loads(plan.read_text(encoding="utf-8"))
    assert len(data["speed"]) == 1 and data["speed"][0]["factor"] == 4.0
    again = tmp_path / "again.mp4"
    assert cli.main([str(long_demo), "--plan", str(plan), "-o", str(again), "--quality", "draft"]) == 0
    assert abs(probe(again).duration - info.duration) < 0.2, "the plan's speed-ups are used"


def test_speed_up_without_zooms_and_bad_values(long_demo, tmp_path):
    out = tmp_path / "plain_fast.mp4"
    assert cli.main([str(long_demo), "-o", str(out), "--no-zoom", "--speed-idle", "3", "--quality", "draft"]) == 0
    assert probe(out).duration < 13
    for bad in (["--speed-idle", "1"], ["--speed-idle", "50"], ["--idle-min", "0"]):
        assert cli.main([str(long_demo), *bad]) == 1
    with pytest.raises(ValueError, match="factor"):
        cli.speedups_from_json({"speed": [{"start": 1, "end": 3, "factor": 99}]})
    with pytest.raises(ValueError, match="after start"):
        cli.speedups_from_json({"speed": [{"start": 3, "end": 1, "factor": 2}]})
