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
