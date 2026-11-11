"""Synthetic screen recordings, drawn with numpy and encoded with ffmpeg: no real screen is ever recorded.

The default script, at 640x360 and 15 fps, 11 seconds:
  0-2 s    a still "desktop" (windows, text-like stripes)
  2-5 s    typing: small dark marks appear one by one in a field at the top right (TYPING box)
  6 s      the whole screen changes (a new page), and stays
  7-9.5 s  a menu opens and items highlight, bottom left (MENU box)
  9.5-11 s still
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from screen_polish.media import ffmpeg_exe

W, H, FPS, DURATION = 640, 360, 15, 11.0
TYPING = (400, 60, 600, 90)  # x0, y0, x1, y1
MENU = (40, 230, 200, 330)


def desktop(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), 236, np.uint8)
    for _ in range(6):  # windows with lines of "text"
        x0, y0 = rng.integers(0, W - 200), rng.integers(0, H - 120)
        w, h = rng.integers(140, 260), rng.integers(80, 160)
        img[y0:y0 + h, x0:x0 + w] = rng.integers(200, 255, 3)
        for ly in range(y0 + 10, y0 + h - 6, 12):
            img[ly:ly + 4, x0 + 8:x0 + w - rng.integers(10, 60)] = 90
    return img


def frame_at(t: float) -> np.ndarray:
    img = desktop(1 if t < 6 else 2)
    if t >= 2:
        x0, y0, x1, y1 = TYPING
        img[y0:y1, x0:x1] = 255
        typed = int(min(t, 5) - 2) * 6 + int(((min(t, 5) - 2) % 1) * 6) if t < 6 else 18
        for k in range(min(typed, 18)):  # one mark every 1/6 s
            cx = x0 + 6 + k * 10
            img[y0 + 8:y1 - 8, cx:cx + 7] = 30
    if 7 <= t < 9.5:
        x0, y0, x1, y1 = MENU
        img[y0:y1, x0:x1] = 250
        item = int((t - 7) * 4) % 5
        for k in range(5):
            img[y0 + 4 + k * 19:y0 + 20 + k * 19, x0 + 4:x1 - 4] = (70, 120, 220) if k == item else (220, 220, 220)
    return img


def write(path: Path, make=frame_at, duration: float = DURATION, fps: int = FPS, size=(W, H), audio: bool = False) -> Path:
    w, h = size
    cmd = [ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(int(duration * fps)):
        proc.stdin.write(make(i / fps).tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return path
