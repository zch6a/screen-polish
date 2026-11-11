# Polish log

## Development — 2026-11-12

**What it is for.** Screen Studio made a style popular: the recording zooms in where you work, on a nice background. It and the open-source apps that follow it (OpenScreen, Recordly, Screenize, ScreenForge, the headless screenstudio-alt) zoom on clicks, so they must make the recording themselves, logging the mouse; screenstudio-alt says plainly that auto-zoom "can't be recovered from pixels". Most screen recordings people already have (OBS, Teams, Zoom, QuickTime, ShareX, Loom, phones) have no click log. Clicks cannot be recovered from pixels, but where the screen changes can, and that is mostly where the viewer should look: the field being typed in, the menu that opened, the dialog, the number that updated.

**Design**
- **Analysis**: decode at 320 px wide, 8 fps, gray; difference with the previous frame; changed pixels (above a noise level of 24) pooled into 8x8 cells, a cell active when 5% of it changed, so H.264 noise and a caret do not count. A moment with more than 30% of cells active is a change of the whole screen (scrolling, a new page, a window switch). In 1.5 s windows (0.5 s hop) the active cells of the other moments give a box (5th–95th percentile of their positions, so a stray cell does not stretch it); consecutive boxes near each other form a segment; a segment with at least 1 s of activity becomes a zoom: margin, the video's aspect ratio, at most `--max-zoom` (2x), and none when it would be less than 1.25x. Neighbouring zooms on the same place merge.
- **Camera**: arrives 0.3 s before the activity, leaves 0.8 s after; 0.8 s moves with smootherstep easing, interpolating the centre and the log of the width, so zooming feels even; pans directly between zooms close in time; across a change of the whole screen it always shows all of it (0.6 s) before zooming in again; ends on the whole screen when there is time.
- **Rendering**: everything constant drawn once (gradient background, blurred rounded-rectangle shadow, corner mask); per frame one Pillow resize of the camera's fractional rectangle and one paste; frames piped to ffmpeg (H.264, yuv420p, even sizes) with the source's audio. GIF through a palette made for it.
- **Plans** are JSON (zooms with times and source-pixel rectangles, and the moments the whole screen changed), written with `--plan-out`, edited, used with `--plan`; scaled when the video's size differs. A click log (`--clicks`) adds clicks as activity.
- **ffmpeg**: the one on PATH, else imageio-ffmpeg's binary (a dependency), so `uvx` works without ffmpeg installed; `SCREEN_POLISH_FFMPEG` overrides. Metadata from ffmpeg's banner (imageio-ffmpeg has no ffprobe), with phone rotation handled.

**Found while building**
- The camera panned straight from one zoom to the next across a page change (the zooms were close in time): the viewer never saw the new page. Found by looking at a contact sheet of frames, not by a test; now the analysis reports the moments the whole screen changed, the camera shows all of it then, and a test checks it.
- The last zoom-out could be cut off by the end of the video; it now starts early enough to end on the whole screen.
- Rendering 1080p took 2.5x real time. Measured: Pillow's affine `transform` cost 72 ms a frame, `resize` with a fractional box 13 ms, and resize also filters properly when scaling down, where the transform aliased. Now about real time (20 s of 1080p30 in 25 s on an i9-12900H).
- ffmpeg's stderr went to a pipe nobody read, which stalls ffmpeg once the pipe fills on a long noisy file; now a temporary file.

**Tests** (12): all on recordings drawn by a script (`tests/synth.py`: a desktop of windows, typing in a field, a new page, a menu), never a real screen: where the zooms land and when; no zoom for a still video or a scrolling one; a click log alone makes a zoom; the camera arrives before the activity, never jumps between frames, pans between close zooms and shows the whole screen when it changes; the rendered frame is zoomed (the typed marks cover 4x the pixels of an unzoomed render, as a 2x zoom should), framed (the background colour in the corner) and keeps the sound and the length; GIF output; plans written, edited, used, rejected when wrong, scaled; plain errors for a missing file, an output that would overwrite the input, a bad colour, a file that is not a video; even, centred layouts for wide and tall videos.

**Open for the polishing rounds**
- Real recordings (only synthetic ones so far, by design: no recording of the user's screen). Public sample screencasts with known licences could be used.
- Idle speed-up, vertical export, a cursor-follow mode with a click log, captions.
- Faster rendering (decode and resample in parallel).
- An independent review.

## Round 1 — 2026-11-12 (part 1: harder recordings)

Synthetic recordings meant to fool the analysis (`tests/synth.py`; still no real screen), and what they found:

- **A blinking caret made a zoom for the whole video.** Every recording with a focused text field has one. A wandering mouse made a zoom too, and a mouse hovering near one spot kept one going for the whole video.
- **A portrait phone recording**: typing along a field too wide for one zoom was mostly missed, and the zoom it got cut the field in half; two zooms overlapped in time (the analysis windows overlap); a merge of two zooms produced a 1.07x "zoom".
- Dark theme, 4K with small text: fine.

**The fix: not every change means "look here".** Each changed cell is now sorted, from a few seconds of small frames kept in memory (the video is still read once, streaming):
- *reverting*: back to a state the cell had in the last 2 s (caret, the mouse moving off, a hover ending): ignored;
- *persistent*: a new state still there 0.5 s later (typed text, an opened menu or dialog, a changed value): an anchor, the only thing that can start a zoom;
- *transient*: the rest (the mouse passing, a highlight moving): keeps a zoom going for up to 3 s after its last anchor, never starts one.

In the first 2 s there is no history to recognise reverting changes, so an anchor there must be bigger than a mouse pointer. Segments also split when their union no longer fits one zoom (so typing along a wide field becomes zooms the camera pans through), zooms start no earlier than the previous one ends, and merging checks that the result is still a zoom.

The synthetic menu was changed: its grey items had the same colour as the desktop behind them, so the menu "opening" was invisible and only a cycling highlight showed. A real menu opening is a clear change; now it is a white panel with a border, and the highlight moves down once.

**Tests**: 17 (+5): caret, wandering mouse, dark theme, a portrait phone recording (zooms one after the other, each a real zoom, together covering the field, no jumps), small text on a 4K screen (marked `slow`).

An independent review is running; its findings go in part 2.

## Round 1 — part 2: the independent review

A reviewer who had not seen the code tried to break it with synthetic recordings (no real screen, no downloads), reproduced 14 of 15 findings, and left the scripts; all 15 are fixed, the reviewer's scripts were rerun against the fixed code, and `tests/test_review.py` holds 20 regression tests (15 fail on the code before the fixes; the other 5 pin what already worked).

- **Audio shorter than the video failed the render** and left a cut file (`-shortest` stopped at the end of the sound). Now the sound is padded with silence (`apad`), so the video is never cut. The README said "the sound is kept as it is"; it is re-encoded to AAC, and now says so.
- **Normal-size text was not seen at 1080p and above.** Analysing at 320 px wide averaged 14 px glyph strokes away, and the tests had used chunky blocks the size of 40 px text. Now the analysis runs at up to 640 px; to keep that cheap, the comparisons with the past and the future are done only for the cells that changed (under 1 ms a frame: about 20 s per hour of video). A test types real text in Pillow's 14 px font on a 1080p screen.
- **A mouse that came to rest started zooms**: the cell under it showed a new picture that lasted. Now a change undone within the next 2 s (the mouse moving on) is no anchor; a zoom needs at least 3 anchor cells in all (counted once each, though the analysis windows overlap: a single 1-cell anchor had been counted three times); at the end of the video, where nothing can be seen undone, only big changes count.
- **The length came from the container**: audio longer than the video, or a WebM recorded in a browser (no duration in the file), left the camera zoomed at the end. Now the video stream is measured (ffmpeg copies it to nowhere, no decoding) when the container's length is missing or disagrees.
- **Variable frame rate** recordings were resampled to their average rate, dropping frames. Now to the timing rate when it is clearly higher (at most 60); 29.97 stays 29.97.
- **The whole screen was shown on a change only between two zooms**, not before the first or after the last. Now always, as far as the activity allows.
- **A plan's height was ignored**: a tall rectangle drawn by hand was cut off. Now the camera shows the whole rectangle.
- **Typing in the first two seconds was missed** (an anchor there had to be big). Now anchors there count when they add up over the window. And a window's box now comes from its anchors when it has some, so that compression noise elsewhere (seen in a dark theme) does not stretch it.
- **Colours**: frames went to YUV with the SD matrix and no tags; HD players assume BT.709 and shifted the colours. Now BT.709, tagged.
- **A failed or interrupted render left a broken file**, and had already overwritten an earlier good one. Now it writes beside the output and renames when complete.
- `--plan-out` could overwrite the input video; plans and click logs with the wrong types, NaN or Infinity crashed or were accepted; an MP3 with cover art produced an empty "video"; videos under 10 px were refused; `--padding 60` or `--width -50` produced a 2x2 picture. All checked now, with plain messages.

The review also confirmed what already worked: a blinking caret, rotation metadata, paths with spaces, Chinese and emoji, bounded memory.

**Tests**: 37 (+20). 1080p rendering is unchanged (20 s of 1080p30 in 24 s).

**Open**
- Real recordings: every test so far uses drawn ones. Public sample screencasts with clear licences would need to be downloaded, which needs the user's go-ahead.
- Idle speed-up, vertical export.

## Round 1 — part 3: what macOS showed

CI failed on macOS only, after part 2: the wandering and resting mouse each got a zoom, the dark theme's first zoom started a second early. Worse encodes on Windows did not reproduce it, so a temporary branch ran a diagnostic on the macOS runner (ffmpeg 9.0 from imageio-ffmpeg):
- the second frame of an encode differed from the first in about a hundred scattered cells, 65 of them taken as anchors: a zoom at 0.1 s. Now nothing in the first half second can start a zoom; real recordings settle then too (a recorder's countdown or toolbar disappearing).
- ten noise cells changed a second before the typing, inside the window that held the typing, and the zoom started at the noise. Now a zoom starts at its first anchor; it still ends at its last change of any kind.

These are real-recording problems that the clean synthetic videos on Windows hid; CI on three systems found them. All six jobs pass now.
