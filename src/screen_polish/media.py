"""ffmpeg: finding it, reading a video's facts, decoding frames into numpy arrays, encoding them back.

ffmpeg on PATH is preferred (usually the newest); otherwise the binary that the imageio-ffmpeg package
ships, so that `uvx screen-polish` works on a machine without ffmpeg.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class MediaError(RuntimeError):
    pass


def ffmpeg_exe() -> str:
    found = os.environ.get("SCREEN_POLISH_FFMPEG") or shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001 - not installed, or no binary for this platform
        raise MediaError("ffmpeg was not found: install it (https://ffmpeg.org/download.html), "
                         "or `pip install imageio-ffmpeg`, or set SCREEN_POLISH_FFMPEG") from e


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool
    rotation: int = 0  # phones store portrait video as landscape plus a rotation; ffmpeg applies it when decoding


def probe(path: str | Path) -> VideoInfo:
    """The facts ffmpeg prints about a file (there is no ffprobe in imageio-ffmpeg, so its banner is read)."""
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"no such file: {path}")
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace")
    info = parse_probe(r.stderr, str(path))
    # The container's duration may be the audio's (longer), or missing (a WebM recorded in a browser). The video
    # stream's own length is measured; it comes out a frame or two short (where the last packet starts), so the
    # container's is kept when the two agree.
    measured = video_duration(path)
    if measured and (not info.duration or abs(info.duration - measured) > 0.5):
        info.duration = measured + 1 / info.fps
    return info


def video_duration(path: str | Path) -> float | None:
    """How long the video stream itself lasts: ffmpeg copies it to nowhere (no decoding, so it is quick) and
    says where it got to."""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", str(path), "-map", "0:V:0", "-c", "copy",
                        "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    times = re.findall(r"out_time_us=(\d+)", r.stdout)
    return int(times[-1]) / 1e6 if times and int(times[-1]) > 0 else None


def parse_probe(text: str, name: str = "input") -> VideoInfo:
    # Not a cover picture: an MP3 or M4A with album art has a "video" stream that is one still image.
    video = next((m.group(0) for m in re.finditer(r"Stream #\S+.*?: Video: .*", text) if "(attached pic)" not in m.group(0)), None)
    if not video:
        raise MediaError(f"{name}: no video stream (is it a video file?)")
    line = video
    size = re.search(r"\b(\d{1,5})x(\d{1,5})\b(?! ?\])", line)
    if not size:
        raise MediaError(f"{name}: could not read the video size")
    w, h = int(size.group(1)), int(size.group(2))
    # Variable frame rate (most screen recorders, and anything that drops repeated frames): "fps" is the
    # average, "tbr" the rate frames are timed at. Decoding resamples to a constant rate: the timing rate when it
    # is clearly higher than the average (so that no frame is dropped), at most 60; else the average (29.97 stays).
    avg = re.search(r"([\d.]+) fps\b", line)
    tbr = re.search(r"([\d.]+) tbr\b", line)
    rates = [float(m.group(1)) for m in (avg, tbr) if m and 1 <= float(m.group(1)) <= 1000]
    fps = rates[0] if rates else 30.0
    if avg and tbr and float(tbr.group(1)) > 1.2 * float(avg.group(1)):
        fps = min(60.0, float(tbr.group(1)))
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0.0
    rot = re.search(r"rotation of (-?[\d.]+) degrees|rotate\s*:\s*(-?\d+)", text)
    rotation = int(float(rot.group(1) or rot.group(2))) % 360 if rot else 0
    if rotation in (90, 270):
        w, h = h, w  # what decoding produces
    return VideoInfo(w, h, round(fps, 3), duration, bool(re.search(r"Stream #\S+.*?: Audio: ", text)), rotation)


def frames(path: str | Path, width: int, height: int, fps: float, gray: bool = False):
    """Decoded frames at a constant `fps`, scaled to width x height: uint8 arrays (H, W) or (H, W, 3)."""
    pix, channels = ("gray", 1) if gray else ("rgb24", 3)
    cmd = [ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:V:0", "-an", "-vf", f"fps={fps},scale={width}:{height}:flags=area",
           "-f", "rawvideo", "-pix_fmt", pix, "-"]
    size = width * height * channels
    # stderr to a file, not a pipe: a pipe nobody reads fills up and stalls ffmpeg on a long, noisy file.
    import tempfile

    with tempfile.TemporaryFile() as errfile:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile, bufsize=size * 4)
        finished = False
        try:
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    finished = True
                    break
                a = np.frombuffer(buf, np.uint8)
                yield a.reshape(height, width) if gray else a.reshape(height, width, 3)
        finally:
            proc.stdout.close()
            if not finished:  # the caller stopped early
                proc.kill()
            proc.wait()
        if finished and proc.returncode != 0:
            errfile.seek(0)
            raise MediaError(f"ffmpeg could not decode {path}: {errfile.read().decode(errors='replace').strip()[-400:]}")


class Encoder:
    """Frames in (RGB, uint8), an H.264 MP4 out, with the source's audio copied alongside."""

    def __init__(self, out: str | Path, width: int, height: int, fps: float, audio_from: str | Path | None = None,
                 crf: int = 18, preset: str = "medium", audio_graph: str | None = None):
        """`audio_graph`: a filter graph from the source's audio ([1:a:0]) to [aout], when the timing changes."""
        cmd = [ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", f"{fps}",
               "-i", "-"]
        if audio_from and audio_graph:
            cmd += ["-i", str(audio_from), "-filter_complex", audio_graph, "-map", "0:v", "-map", "[aout]",
                    "-c:a", "aac", "-b:a", "192k", "-shortest"]
        elif audio_from:
            # apad: audio shorter than the video is padded with silence, so the video is not cut where the sound
            # ends; -shortest then ends both with the video.
            cmd += ["-i", str(audio_from), "-map", "0:v", "-map", "1:a:0?", "-af", "apad", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        # RGB to YUV with the HD matrix, and said so in the file: untagged HD video is shown as BT.709 by most
        # players, and swscale's default (BT.601) would shift the colours.
        cmd += ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                "-movflags", "+faststart", "-f", "mp4", str(out)]
        import tempfile

        self.err = tempfile.TemporaryFile()  # noqa: SIM115 - lives as long as the encoder; a pipe nobody reads would stall ffmpeg
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=self.err)
        self.out = out

    def _error(self) -> str:
        self.err.seek(0)
        return self.err.read().decode(errors="replace").strip()[-400:]

    def write(self, frame: np.ndarray) -> None:
        try:
            self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())
        except (BrokenPipeError, OSError) as e:
            self.proc.wait()
            raise MediaError(f"ffmpeg stopped while writing {self.out}: {self._error()}") from e

    def close(self) -> None:
        with contextlib.suppress(OSError):  # ffmpeg may have closed it already; its exit code says why
            self.proc.stdin.close()
        code = self.proc.wait()
        msg = self._error()
        self.err.close()
        if code != 0:
            raise MediaError(f"ffmpeg could not write {self.out}: {msg}")

    def abort(self) -> None:
        with contextlib.suppress(OSError):
            self.proc.kill()
        self.proc.wait()
        self.err.close()


def to_gif(mp4: str | Path, gif: str | Path, fps: int = 15, max_width: int = 960) -> None:
    """A GIF with its own palette (two passes in one filter graph): far smaller and cleaner than the default.
    Written under another name and renamed when complete, like the MP4."""
    gif = Path(gif)
    partial = gif.with_name(f".{gif.name}.partial")
    graph = (f"fps={fps},scale='min({max_width},iw)':-2:flags=lanczos,split[a][b];"
             "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    try:
        r = subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-i", str(mp4), "-vf", graph, "-f", "gif", str(partial)],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise MediaError(f"ffmpeg could not write {gif}: {r.stderr.strip()[-400:]}")
        os.replace(partial, gif)
    finally:
        partial.unlink(missing_ok=True)
