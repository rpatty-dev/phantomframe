# PhantomFrame

Real-time motion-compensated frame generation for a 30 FPS console captured over HDMI (USB capture card), for 60 FPS (or experimental 120 FPS) on PC.

## Run
```
python gta6_framegen.py          # own frame generation (60 FPS default, HOME = settings)
python gta6_framegen.py --lsfg   # clean 30 FPS passthrough for Lossless Scaling's LSFG
```
Keys: `HOME` settings · `F11` borderless fullscreen · `R` record an 8 s test clip to `recordings/` · `M` mute · `A` cycle audio output.

## How it works
- **Capture thread**: MJPEG 1080p60 via DirectShow; drops the duplicate HDMI frames of a 30 FPS game; timestamps every HDMI frame as an output tick.
- **Flow thread**: DIS optical flow at 960x540 (~5 ms), latest-wins handoff so capture timing is never disturbed.
- **Shader** (OpenGL 3.3): re-anchors flow at the in-between frame, noise deadzone, temporal motion smoothing, bidirectional warp, HUD/text lock, motion blur, MJPEG deblock/denoise/deband + luma CAS.
- **Pacing**: output is clocked by the capture card's 60 Hz cadence, shown after an adaptive delay (p90 capture->flow latency), so each generated frame lands on schedule.

## Measuring quality
```
python tools/bench.py recordings/clip_XXXX.npy
```
Predicts each middle frame from its neighbours with the app's real pipeline and reports PSNR vs the real frame, against repeat/crossfade baselines.
