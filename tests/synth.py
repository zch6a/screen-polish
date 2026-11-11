"""Synthetic screen recordings, drawn with numpy and encoded with ffmpeg: no real screen is ever recorded.

The default script, at 640x360 and 15 fps, 11 seconds:
  0-2 s    a still "desktop" (windows, text-like stripes)
  2-5 s    typing: small dark marks appear one by one in a field at the top right (TYPING box)
  6 s      the whole screen changes (a new page), and stays
  7-9.5 s  a menu opens bottom left (MENU box), and the highlight moves down its items
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
    if 7 <= t < 9.5:  # a menu opens (a white panel with a dark border), and the highlight moves down it
        x0, y0, x1, y1 = MENU
        img[y0:y1, x0:x1] = 40
        img[y0 + 2:y1 - 2, x0 + 2:x1 - 2] = 255
        item = min(4, int((t - 7) * 2))
        for k in range(5):
            top = y0 + 4 + k * 19
            if k == item:
                img[top:top + 16, x0 + 4:x1 - 4] = (70, 120, 220)
            img[top + 6:top + 10, x0 + 10:x1 - 40] = 60 if k != item else 255
    return img


def cursor(img: np.ndarray, x: float, y: float) -> np.ndarray:
    """An arrow mouse pointer, about 12x19 px: white with a black edge."""
    for r in range(19):
        y0, x0 = int(y) + r, int(x)
        x1 = min(img.shape[1], x0 + min(12, r * 12 // 19 + 1))
        if 0 <= y0 < img.shape[0] and x1 > x0:
            img[y0, x0:x1] = 255
            img[y0, x0] = img[y0, x1 - 1] = 0
    return img


def caret(t: float) -> np.ndarray:
    """A focused, empty text field whose caret blinks twice a second; nothing else happens."""
    img = desktop(1)
    img[100:130, 300:500] = 255
    if int(t * 2) % 2 == 0:
        img[105:125, 310:312] = 0
    return img


def wandering_mouse(t: float) -> np.ndarray:
    """The mouse drifting all over a still screen."""
    return cursor(desktop(1), 320 + 280 * np.sin(t * 0.9), 180 + 150 * np.sin(t * 1.3))


def dark(t: float) -> np.ndarray:
    """The default script in a dark theme."""
    return 255 - frame_at(t)


PHONE = (1080, 1920)
PHONE_FIELD = (100, 1500, 980, 1600)


def phone(t: float) -> np.ndarray:
    """A portrait phone recording: typing along a wide field near the bottom, 2-6 s."""
    img = np.full((PHONE[1], PHONE[0], 3), 245, np.uint8)
    x0, y0, x1, y1 = PHONE_FIELD
    img[y0:y1, x0:x1] = 255
    for k in range(min(int(max(0.0, t - 2) * 6), 24)):
        img[y0 + 20:y1 - 20, x0 + 20 + k * 34:x0 + 44 + k * 34] = 30
    return img


UHD = (3840, 2160)
UHD_FIELD = (2000, 300, 3200, 360)


def uhd_small_text(t: float) -> np.ndarray:
    """4K: characters 12 px wide (a normal font on a 4K screen) typed into a field, 1.5-4.5 s."""
    img = np.full((UHD[1], UHD[0], 3), 236, np.uint8)
    x0, y0, x1, y1 = UHD_FIELD
    img[y0:y1, x0:x1] = 255
    for k in range(min(int(max(0.0, t - 1.5) * 8), 24)):
        img[y0 + 15:y1 - 15, x0 + 10 + k * 18:x0 + 22 + k * 18] = 30
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
