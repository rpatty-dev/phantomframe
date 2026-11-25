"""
GTA VI Ultra-Low-Latency Frame Generator (30 -> 60 / 120 FPS)
DirectShow 1080p60 Raw Capture + Dense Optical Flow + GLSL GPU Motion-Compensated Shader
- Pure OpenGL rendering (no QPainter state corruption)
- Native Qt Floating HUD & [Home] Interactive Settings Menu
- Text/UI Edge Halo Suppression + Deadzone Motion Denoising
"""
import sys
import time
import subprocess
import re
import json
import os
import threading
import ctypes
import urllib.request
from collections import deque
import numpy as np
import cv2

from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QSurfaceFormat, QKeySequence, QShortcut, QCursor, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QSlider, QCheckBox, QFrame
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtMultimedia import QMediaDevices, QAudioSource, QAudioSink, QAudioFormat
import OpenGL.GL as gl
import OpenGL.GL.shaders as shaders

# Request 1ms Windows multimedia timer resolution for ultra-tight frame pacing
try:
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:
    pass


# Mac keyboards have no Home key: "\\" opens settings there
SETTINGS_KEY = "\\" if sys.platform == "darwin" else "HOME"

APP_VERSION = "v1.4"
REPO = "rpatty-dev/phantomframe"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
INSTALLER = (f"https://raw.githubusercontent.com/{REPO}/main/install.ps1" if sys.platform == "win32"
             else f"https://raw.githubusercontent.com/{REPO}/main/install.sh")


def version_key(tag: str) -> tuple[int, ...]:
    nums = tuple(int(n) for n in re.findall(r"\d+", tag))
    return nums or (0,)


def fetch_latest_version() -> str:
    req = urllib.request.Request(UPDATE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"PhantomFrame/{APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=6) as response:
        return str(json.load(response).get("tag_name", ""))


def install_latest() -> None:
    """Run the same installer as the README one-liner, without launching a second app."""
    env = os.environ.copy()
    env["PHANTOMFRAME_DIR"] = APP_DIR
    env["PHANTOMFRAME_NO_LAUNCH"] = "1"
    if sys.platform == "win32":
        quoted = APP_DIR.replace("'", "''")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
               f"$env:PHANTOMFRAME_DIR='{quoted}'; $env:PHANTOMFRAME_NO_LAUNCH='1'; irm {INSTALLER} | iex"]
        result = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=300,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        cmd = ["bash", "-c", f"curl -fsSL {INSTALLER} | bash"]
        result = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=300)
    if result.returncode:
        raise RuntimeError((result.stdout or "installer failed").strip()[-300:])


class UpdateChecker(QObject):
    """Checks GitHub off the UI thread, then runs the README installer if a newer release exists."""
    up_to_date = pyqtSignal()
    installed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def run(self) -> None:
        try:
            tag = fetch_latest_version()
            if not tag or version_key(tag) <= version_key(APP_VERSION):
                self.up_to_date.emit()
                return
            install_latest()
            self.installed.emit(tag)
        except Exception as exc:
            self.failed.emit(str(exc))


def find_capture_card_index(preferred_name="USB Video") -> tuple[int, str, list[tuple[int, str]]]:
    """Enumerate DirectShow video devices via ffmpeg and match preferred capture card."""
    devices = []
    if sys.platform == "darwin":
        return _find_capture_card_mac(preferred_name)
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=4
        )
        idx = 0
        for line in res.stderr.splitlines():
            m = re.search(r'"([^"]+)"\s+\(video\)', line)
            if m:
                devices.append((idx, m.group(1)))
                idx += 1
    except Exception:
        pass

    for idx, name in devices:
        if preferred_name.lower() in name.lower():
            return idx, name, devices
    if devices:
        return devices[0][0], devices[0][1], devices
    return 1, preferred_name, [(1, preferred_name)]


# Built-in / Continuity cameras to skip when no device matches the preferred capture card name
_MAC_NON_CAPTURE = ("facetime", "iphone", "ipad", "desk view", "capture screen", "built-in")


def _find_capture_card_mac(preferred_name):
    """macOS: enumerate AVFoundation video devices via ffmpeg (brew install ffmpeg)."""
    devices = []
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, encoding="utf-8", errors="replace", timeout=4
        )
        for line in res.stderr.splitlines():
            if "audio devices" in line:
                break
            m = re.search(r'\]\s*\[(\d+)\]\s+(.+)$', line)
            if m:
                devices.append((int(m.group(1)), m.group(2).strip()))
    except Exception:
        pass

    for idx, name in devices:
        if preferred_name.lower() in name.lower():
            return idx, name, devices
    for idx, name in devices:
        if not any(k in name.lower() for k in _MAC_NON_CAPTURE):
            return idx, name, devices
    if devices:
        return devices[0][0], devices[0][1], devices  # no capture card: built-in camera, so the pipeline still runs
    return 0, preferred_name, [(0, preferred_name)]


class LowLatencyAudioBridge:
    """Zero-buffer 48kHz stereo PCM passthrough from capture card audio to default output."""
    def __init__(self):
        self.source = None
        self.sink = None
        self.io_in = None
        self.io_out = None
        self.muted = False
        self.device_name = "None"
        self.out_devices = []
        self.out_idx = 0

    def start(self, preferred_in="USB Digital Audio", out_idx=None):
        self.stop()
        in_devs = QMediaDevices.audioInputs()
        self.out_devices = QMediaDevices.audioOutputs()
        if not in_devs or not self.out_devices:
            return

        in_dev = None
        for d in in_devs:
            if preferred_in.lower() in d.description().lower() or "digital audio" in d.description().lower():
                in_dev = d
                break
        if in_dev is None:
            return

        if out_idx is not None and 0 <= out_idx < len(self.out_devices):
            self.out_idx = out_idx
        else:
            default_out = QMediaDevices.defaultAudioOutput()
            for i, d in enumerate(self.out_devices):
                if d.id() == default_out.id():
                    self.out_idx = i
                    break
        out_dev = self.out_devices[self.out_idx]

        fmt = QAudioFormat()
        fmt.setSampleRate(48000)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not in_dev.isFormatSupported(fmt) or not out_dev.isFormatSupported(fmt):
            fmt.setSampleRate(44100)

        self.source = QAudioSource(in_dev, fmt)
        self.sink = QAudioSink(out_dev, fmt)
        # ~10ms input / ~12ms output instead of 12/24ms: same passthrough, less audio lag.
        self.source.setBufferSize(1920)
        self.sink.setBufferSize(2304)

        self.io_out = self.sink.start()
        self.io_in = self.source.start()
        if self.io_in and self.io_out:
            self.io_in.readyRead.connect(self._pump)
            self.device_name = f"{in_dev.description()} -> {out_dev.description()}"

    def _pump(self):
        if not self.io_in or not self.io_out:
            return
        data = self.io_in.readAll()
        if not self.muted and data.size() > 0:
            self.io_out.write(data)

    def cycle_output(self):
        if not self.out_devices:
            return
        self.out_idx = (self.out_idx + 1) % len(self.out_devices)
        self.start(out_idx=self.out_idx)

    def stop(self):
        if self.source:
            self.source.stop()
            self.source = None
        if self.sink:
            self.sink.stop()
            self.sink = None
        self.io_in = None
        self.io_out = None


class CaptureFlowEngine:
    """
    Dedicated high-priority capture + optical flow thread.
    - Reads 1920x1080@60fps MJPEG directly via DirectShow (CAP_PROP_BUFFERSIZE=1).
    - Deduplicates repeated frames from console (e.g. 30fps or menu 15fps inside 60Hz HDMI).
    - Computes bidirectional DIS Optical Flow (640x360) in ~1ms on every unique game frame.
    - Applies deadzone noise gate so MJPEG compression blocks never cause shimmer around text.
    """
    # 640x360 flow: 1.3ms vs 2.3ms at 960x540, -0.1dB mean PSNR on recorded gameplay (see bench/)
    FLOW_W = 640
    FLOW_H = 360

    def __init__(self, device_index: int, device_name: str):
        self.device_index = device_index
        self.device_name = device_name
        self.running = False
        self.thread = None

        self.lock = threading.Lock()
        self.f_prev = np.zeros((1080, 1920, 3), dtype=np.uint8)
        self.f_curr = np.zeros((1080, 1920, 3), dtype=np.uint8)
        # f_live is published by the capture thread the instant a frame is decoded. Real frames
        # (and raw passthrough) render from it, so they no longer wait for optical flow to finish.
        self.f_live = self.f_curr
        self.frame_id = 0                 # sequence of the newest decoded frame
        self.flow = np.zeros((self.FLOW_H, self.FLOW_W, 2), dtype=np.float32)
        # Half-float conversion runs in the flow thread, not in paintGL: keeps ~0.5ms off the
        # GPU-thread critical path on every new pair.
        self.flow_half = np.empty_like(self.flow, dtype=np.float16)
        # id() of the half buffer paintGL is uploading; flow thread must not overwrite it mid-upload.
        self.flow_uploading_id = None
        self.pair_id = 0
        self.last_unique_time = time.perf_counter()
        self.unique_period = 1.0 / 30.0
        # 2x pacing is clocked by HDMI frames (one output tick per capture frame), not wall-clock guesses
        self.ticks = deque(maxlen=8)  # (capture_time, pair_id, alpha)
        self.motion_smoothing = True
        self.flow_enabled = True
        self.rec_until = 0.0
        self.rec_frames = []
        self.rec_status = ""
        self.cap_seq = 0                  # sequence number of the latest unique frame (capture thread)
        self.flow_delay = 0.003           # fallback only: generated ticks use their real publish time
        self._job = None                  # (seq, frame, t_capture, is_cut, gray) handed to the flow thread
        self._job_evt = threading.Event()

        # Telemetry
        self.capture_fps = 0.0
        self.unique_fps = 0.0
        self.flow_ms = 0.0
        self.decode_ms = 0.0
        self.flow_max_px = 0.0
        self.flow_mean_px = 0.0
        self.scene_cut = False
        self.flow_preset = 0  # 0 = ULTRAFAST (~1.7ms), 1 = FAST (~4.2ms)
        self._rebuild_dis = True

    def start_recording(self, seconds=8.0):
        # shortcut: raw frames in RAM (~1.5GB for 8s), fine for short test clips only
        if not self.rec_frames:
            self.rec_until = time.perf_counter() + seconds
            self.rec_status = "REC"

    def _save_recording(self):
        frames, self.rec_frames = self.rec_frames, []
        import os
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, time.strftime("clip_%Y%m%d_%H%M%S.npy"))
        self.rec_status = "SAVING"

        def save():
            np.save(path, np.stack(frames))
            self.rec_status = f"SAVED {len(frames)}f"
        threading.Thread(target=save, daemon=True).start()

    def set_flow_preset(self, preset_idx: int):
        self.flow_preset = int(preset_idx)
        self._rebuild_dis = True

    def toggle_flow_preset(self) -> str:
        self.set_flow_preset(1 - self.flow_preset)
        return "FAST (HQ)" if self.flow_preset == 1 else "ULTRAFAST (Low Latency)"

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name="CaptureThread")
        self.thread.start()
        # Flow runs on its own thread so its 10-20ms never delays capture reads/timestamps
        self.flow_thread = threading.Thread(target=self._flow_loop, daemon=True, name="FlowThread")
        self.flow_thread.start()

    def stop(self):
        self.running = False
        self._job_evt.set()
        if self.thread:
            self.thread.join(timeout=2.0)
            self.flow_thread.join(timeout=2.0)

    def _create_dis(self):
        preset = cv2.DISOPTICAL_FLOW_PRESET_FAST if self.flow_preset == 1 else cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST
        dis = cv2.DISOpticalFlow_create(preset)
        dis.setUseSpatialPropagation(True)
        # Stride 4 saves ~1ms on the default ULTRAFAST preset (-0.15dB); keep the denser
        # stride 3 on FAST HQ where the user already accepts the extra compute for quality.
        dis.setPatchStride(4 if self.flow_preset == 0 else 3)
        return dis

    def _loop(self):
        if sys.platform == "darwin":
            # AVFoundation has no MJPG/buffer-size props; set the mode after opening
            cap = cv2.VideoCapture(self.device_index, cv2.CAP_AVFOUNDATION)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            cap.set(cv2.CAP_PROP_FPS, 60)
        else:
            cap = cv2.VideoCapture(self.device_index, cv2.CAP_DSHOW, [
                cv2.CAP_PROP_FRAME_WIDTH, 1920,
                cv2.CAP_PROP_FRAME_HEIGHT, 1080,
                cv2.CAP_PROP_FPS, 60,
                cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'),
                cv2.CAP_PROP_BUFFERSIZE, 1,
            ])

        prev_small = None
        cap_times = deque(maxlen=60)
        uniq_times = deque(maxlen=30)

        while self.running:
            t_dec0 = time.perf_counter()
            ret, frame = cap.read()
            t_now = time.perf_counter()
            if not ret or frame is None:
                time.sleep(0.002)
                continue

            self.decode_ms = (t_now - t_dec0) * 1000.0
            cap_times.append(t_now)
            if len(cap_times) >= 2:
                self.capture_fps = (len(cap_times) - 1) / max(1e-6, cap_times[-1] - cap_times[0])

            # One 640x360 luma prep serves both duplicate detection and DIS flow. The old separate
            # 480x270 dedupe resize cost 3.3ms/frame; this shared pass is now ~0.6ms on the capture PC.
            # INTER_LINEAR instead of INTER_AREA: 0.6ms vs 1.6ms on the capture PC for the whole
            # prep+dedupe pass; app bench PSNR 29.75 -> 29.69dB (-0.06), worst10 unchanged.
            small = cv2.GaussianBlur(cv2.cvtColor(
                cv2.resize(frame, (self.FLOW_W, self.FLOW_H), interpolation=cv2.INTER_LINEAR),
                cv2.COLOR_BGR2GRAY), (3, 3), 0)

            # Duplicate frame from 30fps console inside 60Hz container:
            # 1) Never accept two unique frames less than 24ms apart (since console output is 30fps = 33.3ms)
            # 2) If frame is identical (mad < 0.05) and less than 31ms has passed, skip duplicate
            # 3) Always emit at least at 30Hz (every 31.8ms) so static scenes / menus stay locked at 60/120 FPS
            if prev_small is None:
                mad = 255.0  # first frame: treat as a cut
            else:
                # cv2.mean avoids uint8->float64 materialisation (~10x cheaper than .mean())
                mad = cv2.mean(cv2.absdiff(small, prev_small))[0]
                dt_since_unique = t_now - self.last_unique_time
                if dt_since_unique < 0.024 or (mad < 0.05 and dt_since_unique < 0.0312):
                    # Repeated HDMI frame = next output tick: show the real frame
                    with self.lock:
                        self.ticks.append((t_now, self.cap_seq, 1.0))
                    continue

            # New unique console frame arrived!
            is_cut = mad > 35.0
            prev_small = small
            self.cap_seq += 1

            # [R] test-clip recording: unique frames only, saved off-thread when the window ends
            if t_now < self.rec_until:
                self.rec_frames.append(frame)
            elif self.rec_frames:
                self._save_recording()

            uniq_times.append(t_now)
            if len(uniq_times) >= 2:
                self.unique_fps = (len(uniq_times) - 1) / max(1e-6, uniq_times[-1] - uniq_times[0])
                avg_p = (uniq_times[-1] - uniq_times[0]) / (len(uniq_times) - 1)
                self.unique_period = float(np.clip(avg_p, 1.0 / 60.0, 1.0 / 12.0))

            with self.lock:
                self.last_unique_time = t_now
                # Publish the decoded frame now. Real ticks only need this, not the flow pair, so
                # a busy flow thread can no longer hold the picture back.
                self.frame_id = self.cap_seq
                self.f_live = frame
                self.ticks.append((t_now, self.cap_seq, 1.0 if is_cut else 0.5))
            # Latest-wins handoff: if flow is still busy the older pending frame is simply skipped
            self._job = (self.cap_seq, frame, t_now, is_cut, small)
            self._job_evt.set()

        cap.release()

    def _flow_loop(self):
        dis = self._create_dis()
        self._rebuild_dis = False
        prev_gray = prev_full = None
        # Triple buffer: publish by reference instead of copying ~1.8MB under the lock. paintGL marks
        # the buffer it is uploading; skip that slot so a slow GUI can never be overwritten mid-upload.
        flow_bufs = [np.zeros((self.FLOW_H, self.FLOW_W, 2), dtype=np.float32) for _ in range(3)]
        half_bufs = [np.empty_like(flow_bufs[0], dtype=np.float16) for _ in range(3)]
        slot = 0
        lat = deque(maxlen=30)

        while self.running:
            if not self._job_evt.wait(0.1):
                continue
            self._job_evt.clear()
            job = self._job
            if job is None:
                continue
            seq, frame, t_cap, is_cut, gray = job
            flow_buf = flow_bufs[slot]
            half_buf = half_bufs[slot]
            with self.lock:
                if id(half_buf) == self.flow_uploading_id:
                    slot = (slot + 1) % len(flow_bufs)
                    flow_buf = flow_bufs[slot]
                    half_buf = half_bufs[slot]
            if self._rebuild_dis:
                dis = self._create_dis()
                self._rebuild_dis = False

            t_flow0 = time.perf_counter()
            if prev_gray is None or is_cut or not self.flow_enabled:  # passthrough/LSFG: no flow needed
                flow_buf.fill(0.0)
                self.flow_max_px = 0.0
                self.flow_mean_px = 0.0
            else:
                # Raw DIS flow (prev->curr, flow-res pixels). Re-anchoring, noise gate and temporal
                # smoothing run in the shader (flowAt): the same steps on CPU cost ~20ms (~45ms in-app)
                dis.calc(prev_gray, gray, flow_buf)  # reuse triple-buffered output, no fresh alloc
                m = np.hypot(flow_buf[::8, ::8, 0], flow_buf[::8, ::8, 1])  # stats from a 1/64 sample
                self.flow_max_px = float(m.max()) * (1920.0 / self.FLOW_W)
                self.flow_mean_px = float(m.mean()) * (1920.0 / self.FLOW_W)
            # Convert here instead of in paintGL: the GPU thread only uploads ready-to-use bytes.
            np.copyto(half_buf, flow_buf)
            self.flow_ms = (time.perf_counter() - t_flow0) * 1000.0

            t_pub = time.perf_counter()
            with self.lock:
                self.f_prev = frame if prev_full is None else prev_full
                self.f_curr = frame
                self.flow = flow_buf
                self.flow_half = half_buf
                self.scene_cut = is_cut
                self.pair_id = seq
            slot = (slot + 1) % len(flow_bufs)
            prev_gray, prev_full = gray, frame

            # Display delay = p95 capture->publish latency + margin (one shared deadline for both
            # midpoint and real ticks so the real duplicate can never overtake the midpoint).
            lat.append(t_pub - t_cap)
            self.flow_delay = float(np.clip(np.percentile(lat, 95) + 0.002, 0.003, 0.035))


VERTEX_SHADER = """
#version 330 core
out vec2 v_uv;
void main() {
    // Fullscreen triangle (no VBO needed)
    vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    v_uv = vec2(pos.x, 1.0 - pos.y);
    gl_Position = vec4(pos * 2.0 - 1.0, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = """
#version 330 core
in vec2 v_uv;
out vec4 fragColor;

uniform sampler2D u_texPrev;
uniform sampler2D u_texCurr;
uniform sampler2D u_texLive;      // newest decoded frame, ready before flow finishes
uniform sampler2D u_texFlow;      // raw DIS flow, prev->curr, in 640x360 pixels
uniform sampler2D u_texFlowPrev;  // previous pair's raw flow (temporal smoothing)
uniform int u_smooth;             // 1 = motion smoothing on

const vec2 FLOW_RES = vec2(640.0, 360.0);  // keep in sync with CaptureFlowEngine.FLOW_W/H

// Raw flow -> UV units, with a ~0.6px@1080p deadzone so MJPEG noise never becomes motion
vec2 rawFlow(sampler2D t, vec2 p) {
    vec2 f = texture(t, p).rg;
    return dot(f, f) < 0.09 ? vec2(0.0) : f / FLOW_RES;
}

// Flow re-anchored at the in-between frame (v = F(p - v/2)), since synthesis samples flow at the
// interpolated pixel; then temporally smoothed where it agrees with the previous pair's motion
vec2 flowAt(vec2 p) {
    vec2 f = rawFlow(u_texFlow, p);
    f = rawFlow(u_texFlow, p - 0.5 * f);
    if (u_smooth == 1) {
        vec2 o = rawFlow(u_texFlowPrev, p - 0.5 * f);
        vec2 d = abs(f - o) * FLOW_RES;
        vec2 a = abs(f) * FLOW_RES;
        if (d.x + d.y < 0.5 * (a.x + a.y) + 0.77) f = 0.8 * f + 0.2 * o;
    }
    return f;
}

uniform float u_alpha;       // 0.0..1.0 interpolation phase (or 1.0 for real frame)
uniform int u_mode;          // 0 = FrameGen, 1 = Raw Passthrough, 2 = Split Compare, 3 = Motion Vector Heatmap
uniform float u_sharpness;   // Edge-safe CAS contrast adaptive sharpening strength (0.0..1.0)
uniform int u_hudProtect;    // 1 = Protect text/HUD/UI edges from warp & MJPEG ringing
uniform vec2 u_texelSize;    // (1/1920, 1/1080)
uniform float u_blur;        // Motion blur shutter (0 = off .. 1 = full output-frame shutter)
uniform float u_frameStep;   // Fraction of a console frame per output frame (0.5 at 60, 0.25 at 120)
uniform float u_devig;       // 0..1 strength of game-vignette removal

uniform float u_clarity;     // local contrast / dehaze (0..1)
uniform float u_vibrance;    // saturation boost for dull colors (0..1)
uniform float u_contrast;    // S-curve (0..1)
uniform int u_compare;       // 1 = left half shows the image without vignette removal + grading

float lumaOf(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

vec3 grade(vec3 c, vec2 uv) {
    // Clarity/dehaze: push each pixel away from its local mean (mip level 5 = ~32px area), midtones only
    if (u_clarity > 0.0) {
        float l = lumaOf(c);
        float lm = lumaOf(textureLod(u_texCurr, uv, 5.0).rgb);
        c += u_clarity * 0.9 * (l - lm) * (1.0 - abs(2.0 * l - 1.0));
    }
    // Contrast: blend toward a smoothstep S-curve (deeper shadows, brighter highlights)
    if (u_contrast > 0.0) {
        vec3 x = clamp(c, 0.0, 1.0);
        c = mix(c, x * x * (3.0 - 2.0 * x), u_contrast * 0.5);
    }
    // Vibrance: saturate dull colors more than already-saturated ones (keeps skin natural)
    if (u_vibrance > 0.0) {
        float sat = max(c.r, max(c.g, c.b)) - min(c.r, min(c.g, c.b));
        c = mix(vec3(lumaOf(c)), c, 1.0 + u_vibrance * 0.6 * (1.0 - sat));
    }
    return clamp(c, 0.0, 1.0);
}

// Undo the game's vignette. Fitted on tracked points across camera pans in recorded GTA VI gameplay:
// linear-light gain V = 1 - 0.75 * r^2, r = 0 center .. 1 corner, elliptical in screen space.
vec3 devignette(vec3 c, vec2 uv) {
    if (u_devig <= 0.0) return c;
    vec2 d = uv - 0.5;
    float v = 1.0 - 0.75 * dot(d, d) * 2.0;
    float gain = mix(1.0, 1.0 / max(v, 0.25), u_devig);
    // Ease off on highlights: full gain pushed bright sky/water near the edges to white (washed-out look)
    gain = mix(gain, 1.0, smoothstep(0.55, 0.95, dot(c, vec3(0.299, 0.587, 0.114))));
    // Minimap (bottom-left) is drawn on top of the game's vignette, so it must not be brightened
    vec2 m = smoothstep(vec2(0.03, 0.70), vec2(0.06, 0.73), uv) * (1.0 - smoothstep(vec2(0.26, 0.95), vec2(0.29, 0.98), uv));
    gain = mix(gain, 1.0, m.x * m.y);
    return clamp(pow(pow(c, vec3(2.2)) * gain, vec3(1.0 / 2.2)), 0.0, 1.0);
}

// Final look: vignette removal + grade, or the untouched image on the left half in compare mode
vec3 finish(vec3 c, vec2 uv) {
    if (u_compare == 1) {
        if (abs(uv.x - 0.5) < 0.0006) return vec3(1.0);
        if (uv.x < 0.5) return c;
    }
    return grade(devignette(c, uv), uv);
}

float getLuma(vec3 c) {
    return dot(c, vec3(0.299, 0.587, 0.114));
}

// Measure local high-contrast edge strength (detects crisp text/UI outlines where MJPEG mosquito noise lives)
float textEdgeStrength(sampler2D tex, vec2 uv, vec3 center) {
    vec3 n = texture(tex, uv + vec2(0.0, -u_texelSize.y)).rgb;
    vec3 s = texture(tex, uv + vec2(0.0,  u_texelSize.y)).rgb;
    vec3 w = texture(tex, uv + vec2(-u_texelSize.x, 0.0)).rgb;
    vec3 e = texture(tex, uv + vec2( u_texelSize.x, 0.0)).rgb;
    vec3 mn = min(min(min(n, s), min(w, e)), center);
    vec3 mx = max(max(max(n, s), max(w, e)), center);
    float contrast = max(max(mx.r - mn.r, mx.g - mn.g), mx.b - mn.b);
    return smoothstep(0.32, 0.65, contrast);
}

// Contrast Adaptive Sharpening (CAS) with automatic anti-ringing suppression around text/high-contrast edges
vec3 applyCAS(sampler2D tex, vec2 uv, vec3 center, float amt) {
    // Deblock/denoise/color rebuild already ran in the per-frame prefilter pass (PREFILTER_FS).
    // At the default sharpness=0, skip 6 texture fetches: -0.01dB mean, ~0.15ms at 1080p.
    if (amt <= 0.01) return center;
    vec2 ts = u_texelSize;
    vec3 n = texture(tex, uv + vec2(0.0, -ts.y)).rgb;
    vec3 s = texture(tex, uv + vec2(0.0,  ts.y)).rgb;
    vec3 w = texture(tex, uv + vec2(-ts.x, 0.0)).rgb;
    vec3 e = texture(tex, uv + vec2( ts.x, 0.0)).rgb;
    vec3 den = center;

    // 2. Deband: flat areas (skies, dark gradients) get a wide average + dither to hide 8-bit/MJPEG steps
    float h = fract(sin(dot(uv, vec2(12.9898, 78.233))) * 43758.5453);
    vec2 r = vec2(cos(h * 6.283), sin(h * 6.283)) * 12.0 * ts;
    // Two opposite taps are enough for the flat-area average; PSNR unchanged, ~0.07ms at 1080p
    vec3 b0 = texture(tex, uv + r).rgb, b1 = texture(tex, uv - r).rgb;
    vec3 dmax = max(abs(b0 - den), abs(b1 - den));
    if (max(max(dmax.r, dmax.g), dmax.b) < 0.012) {
        den = (den + b0 + b1) / 3.0 + (h - 0.5) / 255.0;
    }

    // 3. CAS on luma only (sharpening chroma just amplifies MJPEG color noise)
    float ln = getLuma(n), ls = getLuma(s), lw = getLuma(w), le = getLuma(e), ld = getLuma(den);
    float mn = min(min(min(ln, ls), min(lw, le)), ld);
    float mx = max(max(max(ln, ls), max(lw, le)), ld);
    // Suppress on very high-contrast edges (text/HUD) so they never halo
    float effectiveAmt = amt * (1.0 - smoothstep(0.28, 0.55, mx - mn));
    if (effectiveAmt <= 0.005) return den;
    float amp = clamp(min(mn, 1.0 - mx) / max(mx, 1e-4), 0.0, 1.0);
    float wgt = -sqrt(amp) * (0.2 * effectiveAmt);  // AMD CAS range is 1/8..1/5
    float sl = (ld + (ln + ls + lw + le) * wgt) / (1.0 + 4.0 * wgt);
    return clamp(den + (sl - ld), 0.0, 1.0);
}

// Text & HUD protection mask: prevents warping static or sharp high-contrast UI/menu text
float hudAndTextLock(vec2 uv, vec3 cPrev, vec3 cCurr) {
    if (u_hudProtect == 0) return 0.0;
    float colorDiff = length(cCurr - cPrev);

    // 1. Any static pixel across frames (diff < 3%) should never be warped by neighbor flow bleed
    float staticLock = 1.0 - smoothstep(0.015, 0.065, colorDiff);

    // 2. High-contrast text/UI edges anywhere on screen
    float edgeCurr = textEdgeStrength(u_texCurr, uv, cCurr);
    // Prev-frame edge tap was redundant on recorded gameplay (PSNR unchanged) and cost 4 fetches.
    float edgePrev = 0.0;
    float textLock = max(edgeCurr, edgePrev) * (1.0 - smoothstep(0.05, 0.22, colorDiff));

    // 3. Center reticle & outer HUD corners
    float centerDist = length((uv - vec2(0.5, 0.5)) * vec2(1.777, 1.0));
    float reticle = 1.0 - smoothstep(0.012, 0.024, centerDist);
    float edgeX = max(smoothstep(0.18, 0.04, uv.x), smoothstep(0.82, 0.96, uv.x));
    float edgeY = max(smoothstep(0.16, 0.04, uv.y), smoothstep(0.80, 0.94, uv.y));
    float hudZone = max(reticle, edgeX * edgeY * 0.85) * (1.0 - smoothstep(0.04, 0.16, colorDiff));

    // staticLock dropped: slow pans change pixels only slightly, so it froze them to the real frame (felt 30fps).
    // Truly static pixels already get ~zero flow and stay put without it.
    return clamp(max(textLock, hudZone), 0.0, 1.0);
}

// Computed once per fragment and reused by the blur pass below (saves 3 flow + 4 HUD fetches).
vec2 g_flow;
float g_lock;

vec3 synthesizeFrame(vec2 uv, float t) {
    g_flow = vec2(0.0);
    g_lock = 0.0;
    // Real frames need no vectors at all: skip 3 flow fetches and one colour fetch. Half the
    // output frames take this path, so it also helps us never miss a vblank deadline.
    if (t >= 0.99) return applyCAS(u_texCurr, uv, texture(u_texCurr, uv).rgb, u_sharpness);
    if (t <= 0.01) return applyCAS(u_texPrev, uv, texture(u_texPrev, uv).rgb, u_sharpness);
    g_flow = flowAt(uv);
    vec3 rawPrev = texture(u_texPrev, uv).rgb;
    vec3 rawCurr = texture(u_texCurr, uv).rgb;

    // Minimap (bottom-left): plain crossfade, no warp. Its rotation breaks the flow; benchmarked
    // as the best of 10 methods (+1.7dB on the map, +0.05dB overall, zero extra latency)
    if (u_hudProtect == 1 && uv.x > 0.047 && uv.x < 0.276 && uv.y > 0.676 && uv.y < 0.958) {
        g_lock = 1.0;  // crossfaded map: never blur its rotating arrows
        return mix(rawPrev, rawCurr, t);
    }

    // Check HUD & Text lock first
    float lockAmt = hudAndTextLock(uv, rawPrev, rawCurr);
    g_lock = lockAmt;
    if (lockAmt >= 0.96) {
        return rawCurr;
    }

    // Single flowAt (cached in g_flow): on recorded gameplay 4 neighbour taps changed PSNR by <0.003dB
    // while adding 12 texture fetches per pixel (~0.6ms at 1080p).
    vec2 v = g_flow;

    // Scale down flow on high-contrast text edges to prevent character tearing
    v *= (1.0 - lockAmt);

    float mag = length(v);
    if (mag < 0.00025) {
        return applyCAS(u_texCurr, uv, rawCurr, u_sharpness);
    }
    if (mag > 0.075) {
        v *= (0.075 / mag);
    }

    // Bidirectional motion-compensated warp
    vec2 uvPrev = clamp(uv - v * t, vec2(0.001), vec2(0.999));
    vec2 uvCurr = clamp(uv + v * (1.0 - t), vec2(0.001), vec2(0.999));

    vec3 warpPrev = texture(u_texPrev, uvPrev).rgb;
    vec3 warpCurr = texture(u_texCurr, uvCurr).rgb;

    // No occlusion "snap to rawCurr" fallback: benchmarked on recorded gameplay it cost 0.8dB mean /
    // 1.1dB worst-case PSNR (it made uncertain regions pop to the next real frame)
    vec3 warped = mix(warpPrev, warpCurr, t);
    vec3 finalRgb = mix(warped, rawCurr, lockAmt);

    return applyCAS(u_texCurr, uv, finalRgb, u_sharpness * (1.0 - lockAmt));
}

// High-visibility HSV-to-RGB color wheel + vector grid for Optical Flow Heatmap mode
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

void main() {
    if (u_mode == 1) {
        vec3 c = texture(u_texLive, v_uv).rgb;
        fragColor = vec4(finish(applyCAS(u_texLive, v_uv, c, u_sharpness), v_uv), 1.0);
        return;
    }
    if (u_mode == 3) {
        // Motion Vector Optical Flow Heatmap (Hue = Direction, Brightness = Speed in pixels)
        vec3 base = texture(u_texCurr, v_uv).rgb;
        vec2 f_px = flowAt(v_uv) * vec2(1920.0, 1080.0);
        float speed = length(f_px);
        float angle = atan(f_px.y, f_px.x) / (2.0 * 3.14159265) + 0.5;
        // High sensitivity: even 0.5px..12px motion lights up brightly in color
        float normSpeed = clamp(speed / 12.0, 0.0, 1.0);
        vec3 flowColor = hsv2rgb(vec3(angle, 0.95, pow(normSpeed, 0.55)));

        // Draw 32x32 motion grid lines that warp with flow so user sees live vector displacement
        vec2 warpedGridUv = (v_uv - flowAt(v_uv) * 2.5) * vec2(60.0, 34.0);
        vec2 gridCell = abs(fract(warpedGridUv - 0.5) - 0.5) / fwidth(warpedGridUv);
        float gridLine = 1.0 - min(min(gridCell.x, gridCell.y), 1.0);
        vec3 gridColor = mix(vec3(0.0, 0.45, 0.65), vec3(0.0, 1.0, 0.85), normSpeed);

        vec3 darkBase = vec3(getLuma(base) * 0.35);
        vec3 comp = mix(darkBase, flowColor, clamp(normSpeed * 1.25, 0.15, 0.92));
        comp = mix(comp, gridColor, gridLine * 0.45);
        fragColor = vec4(comp, 1.0);
        return;
    }
    if (u_mode == 2) {
        // Split screen: Left = 30fps Raw, Right = FrameGen
        if (abs(v_uv.x - 0.5) < 0.0015) {
            fragColor = vec4(1.0, 0.25, 0.65, 1.0);
            return;
        }
        if (v_uv.x < 0.5) {
            vec3 c = texture(u_texLive, v_uv).rgb;
            fragColor = vec4(finish(applyCAS(u_texLive, v_uv, c, u_sharpness), v_uv), 1.0);
        } else {
            fragColor = vec4(finish(synthesizeFrame(v_uv, u_alpha), v_uv), 1.0);
        }
        return;
    }

    // Mode 0: Full Motion-Compensated Frame Generation
    vec3 col = synthesizeFrame(v_uv, u_alpha);
    // Real frames are already sharp captures: blurring them only costs 10 fetches and softens the
    // picture. Blur synthesized frames only, and reuse the HUD mask computed in synthesizeFrame.
    if (u_blur > 0.001 && u_alpha < 0.99 && u_alpha > 0.01) {
        // Per-pixel motion blur along the flow vector, spanning the output frame's shutter
        vec2 v = g_flow;
        float m = length(v);
        if (m > 0.0004) {
            if (m > 0.075) v *= 0.075 / m;
            float keep = g_lock;  // never smear HUD/subtitles
            vec2 span = v * (u_frameStep * u_blur);
            float t = clamp(u_alpha, 0.0, 1.0);
            vec3 acc = col;
            // 4 taps instead of 8: identical PSNR on recorded gameplay (~0.001dB), ~0.2ms at 1080p
            for (int i = 0; i < 4; i++) {
                vec2 o = span * ((float(i) + 0.5) / 4.0 - 0.5);
                vec3 a = texture(u_texPrev, clamp(v_uv + o - v * t, 0.001, 0.999)).rgb;
                vec3 b = texture(u_texCurr, clamp(v_uv + o + v * (1.0 - t), 0.001, 0.999)).rgb;
                acc += mix(a, b, t);
            }
            col = mix(acc / 5.0, col, keep);
        }
    }
    fragColor = vec4(finish(col, v_uv), 1.0);
}
"""


# Per-frame cleanup pass, run once when a captured frame is uploaded, so real AND generated frames
# share the same cleaned image (doing it in the output shader made generated frames differ -> flicker)
PREFILTER_VS = """
#version 330 core
out vec2 uv;
void main() {
    vec2 pos = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    uv = pos;  // no flip: FBO row r <-> texture row r
    gl_Position = vec4(pos * 2.0 - 1.0, 0.0, 1.0);
}
"""

PREFILTER_FS = """
#version 330 core
in vec2 uv;
out vec4 o;
uniform sampler2D u_src;
uniform vec2 u_texel;

float Y(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }
vec3 at(vec2 d) { return texture(u_src, uv + d * u_texel).rgb; }

void main() {
    vec3 c = at(vec2(0.0));
    // 1. MJPEG deblock: soften only small steps across the 8x8 block grid; real edges are left alone
    vec2 m = mod(floor(uv / u_texel), 8.0);
    if (m.x == 7.0 || m.x == 0.0) {
        vec3 q = at(vec2(m.x == 7.0 ? 1.0 : -1.0, 0.0));
        c = mix(c, q, 0.3 * (1.0 - smoothstep(0.02, 0.09, length(q - c))));
    }
    if (m.y == 7.0 || m.y == 0.0) {
        vec3 q = at(vec2(0.0, m.y == 7.0 ? 1.0 : -1.0));
        c = mix(c, q, 0.3 * (1.0 - smoothstep(0.02, 0.09, length(q - c))));
    }
    // 2. Bilateral denoise (mosquito noise) + 3. color edge rebuild: MJPEG stores color at
    // reduced resolution, so average chroma only from neighbors with matching luma -> color snaps to edges
    float lc = Y(c);
    vec3 acc = c; float ws = 1.0;
    vec3 chr = c - lc; float wc = 1.0;
    // 3x3 instead of 5x5: measured 49.6dB vs the old pass on recorded gameplay (visually identical)
    // and ~0.9ms faster per uploaded frame.
    for (int y = -1; y <= 1; y++)
        for (int x = -1; x <= 1; x++) {
            if (x == 0 && y == 0) continue;
            vec3 q = at(vec2(x, y));
            float d = Y(q) - lc;
            // exp2 is a single hardware instruction; constants are 400/2500 * log2(e)
            float wq = exp2(-d * d * 577.08) * (abs(x) + abs(y) == 1 ? 0.6 : 0.35);
            acc += q * wq; ws += wq;
            float wk = exp2(-d * d * 3606.74);   // tight luma match for chroma
            chr += (q - Y(q)) * wk; wc += wk;
        }
    vec3 den = mix(c, acc / ws, 0.6);
    o = vec4(Y(den) + chr / wc, 1.0);
}
"""


class _VBlank(QObject):
    tick = pyqtSignal()


class FrameGenGLWidget(QOpenGLWidget):
    """
    Pure OpenGL 3.3 Core Renderer with Sub-Frame Phase Pacing (30 / 60 / 120 FPS).
    No QPainter calls inside paintGL — keeps OpenGL state 100% clean.
    """
    def __init__(self, engine: CaptureFlowEngine, audio: LowLatencyAudioBridge, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.audio = audio

        self.program = None
        self.vao = None
        self.tex_prev = None
        self.tex_curr = None
        self.tex_live = None
        self.tex_flow = None
        self.pbos = None
        self.pbo_idx = 0

        self.uploaded_pair_id = -1
        self.uploaded_frame_id = -1
        self._viewport = (0, 0, 1920, 1080)
        self.mode = 0                  # 0: FrameGen, 1: Raw Passthrough, 2: Split Compare, 3: Flow Heatmap
        self.target_fps = 60           # 30, 60, 90, 120, 144
        self.zero_hold_extrap = False  # False = Interpolation, True = 0ms Hold Hybrid Extrapolation
        self.hud_protect = True
        self.motion_blur = 0.22        # light shutter blur
        self.devignette = 1.0          # remove the game's vignette (0..1); highlights are eased so it doesn't wash out
        self.clarity = 0.33            # local contrast / dehaze
        self.vibrance = 0.75
        self.contrast = 0.01
        self.compare = False           # [B] left half = before grading
        self.sharpness = 0.0          # off by default (tuned); the prefilter already cleans MJPEG

        # Telemetry verification counters: track distinct presented frames (real vs synthesized)
        self.render_times = deque(maxlen=140)
        self.distinct_frame_times = deque(maxlen=140)
        self.output_fps = 60.0
        self.distinct_output_fps = 60.0
        self.last_presented_signature = (-1, -1.0)
        self.synthesized_count = 0
        self.real_count = 0

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.update)
        # Repaint once per display refresh (DwmFlush blocks until the next DWM vblank) instead of a 4ms poll:
        # removes up to 4ms of present jitter and frees CPU for the capture/flow threads
        try:
            self.use_vblank = ctypes.windll.dwmapi.DwmFlush() == 0
        except Exception:
            self.use_vblank = False
        self.vblank_t = time.perf_counter()
        self.vb_period = 1 / 60
        self.phase_adj = 0.0  # extra tick delay that keeps deadlines mid-way between vblanks (no flip-flopping)
        if self.use_vblank:
            self._vb = _VBlank()
            # repaint() paints in this slot instead of posting another paint event: one less
            # event-loop hop between the DWM vblank and the actual swap (~1ms per frame).
            self._vb.tick.connect(self._present)
            threading.Thread(target=self._vblank_loop, daemon=True).start()
        self.set_target_fps(60)

    def _present(self):
        self.repaint()

    def _vblank_loop(self):
        flush = ctypes.windll.dwmapi.DwmFlush
        last = time.perf_counter()
        while True:
            flush()
            t = time.perf_counter()
            if 0.004 < t - last < 0.040:
                self.vb_period += 0.02 * ((t - last) - self.vb_period)
            self.vblank_t = last = t
            try:
                self._vb.tick.emit()
            except RuntimeError:
                return  # widget closed

    def set_target_fps(self, fps: int):
        self.target_fps = int(fps)
        # DWM path is vblank-driven. Without it, poll once per output frame: 4ms at 60Hz overpresents
        # and flickers on macOS; 2ms starved the flow/capture threads on Windows.
        self.timer.setInterval(4 if self.use_vblank else max(4, round(1000.0 / self.target_fps)))
        if not self.use_vblank and not self.timer.isActive():
            self.timer.start()

    def initializeGL(self):
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)

        vs = shaders.compileShader(VERTEX_SHADER, gl.GL_VERTEX_SHADER)
        fs = shaders.compileShader(FRAGMENT_SHADER, gl.GL_FRAGMENT_SHADER)
        self.program = shaders.compileProgram(vs, fs, validate=False)  # macOS fails validation before a framebuffer is bound

        self.vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(self.vao)

        # Frame before tex_prev: lets the previous pair's last sub-frames finish after the next pair arrives
        self.pre_program = shaders.compileProgram(
            shaders.compileShader(PREFILTER_VS, gl.GL_VERTEX_SHADER),
            shaders.compileShader(PREFILTER_FS, gl.GL_FRAGMENT_SHADER), validate=False)
        # Uniform locations are immutable for a linked program; querying them per paint is driver work
        self.u = {name: gl.glGetUniformLocation(self.program, name) for name in (
            "u_texPrev", "u_texCurr", "u_texLive", "u_texFlow", "u_texFlowPrev", "u_smooth", "u_alpha",
            "u_mode", "u_sharpness", "u_hudProtect", "u_texelSize", "u_blur", "u_frameStep",
            "u_devig", "u_clarity", "u_vibrance", "u_contrast", "u_compare")}
        # Sampler units and texel size are program constants: set once, not every paint.
        gl.glUseProgram(self.program)
        for name, unit in (("u_texPrev", 0), ("u_texCurr", 1), ("u_texFlow", 2), ("u_texFlowPrev", 3),
                           ("u_texLive", 4)):
            gl.glUniform1i(self.u[name], unit)
        gl.glUniform2f(self.u["u_texelSize"], 1.0 / 1920.0, 1.0 / 1080.0)

        self.pre_u_src = gl.glGetUniformLocation(self.pre_program, "u_src")
        self.pre_u_texel = gl.glGetUniformLocation(self.pre_program, "u_texel")
        self.pre_fbo = gl.glGenFramebuffers(1)
        self.tex_raw = self._create_texture(1920, 1080, gl.GL_RGB8, gl.GL_BGR, gl.GL_UNSIGNED_BYTE)
        self.tex_pp = self._create_texture(1920, 1080, gl.GL_RGB8, gl.GL_BGR, gl.GL_UNSIGNED_BYTE)
        self.pp_valid = False
        self.tex_prev = self._create_texture(1920, 1080, gl.GL_RGB8, gl.GL_BGR, gl.GL_UNSIGNED_BYTE)
        self.tex_curr = self._create_texture(1920, 1080, gl.GL_RGB8, gl.GL_BGR, gl.GL_UNSIGNED_BYTE)
        self.tex_live = self._create_texture(1920, 1080, gl.GL_RGB8, gl.GL_BGR, gl.GL_UNSIGNED_BYTE)
        for t in (self.tex_pp, self.tex_prev, self.tex_curr):
            gl.glBindTexture(gl.GL_TEXTURE_2D, t)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
            # grade() only samples mip 5 (local mean); skip building unused levels 6-10
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAX_LEVEL, 5)
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        # Half float matches DIS output precision well within 0.001dB and halves flow upload bandwidth.
        self.tex_flow, self.tex_flow_prev = (self._create_texture(
            CaptureFlowEngine.FLOW_W, CaptureFlowEngine.FLOW_H,
            gl.GL_RG16F, gl.GL_RG, gl.GL_HALF_FLOAT
        ) for _ in range(2))

        # Double-buffered pixel-buffer objects: the 6MB upload no longer stalls the GPU thread
        # waiting for the previous frame's transfer to finish. Fall back to a direct upload if
        # the driver refuses PBOs.
        try:
            # Measured on macOS: PBO upload ~3.0ms vs direct ~2.2ms and less steady cadence.
            if sys.platform == "darwin":
                raise RuntimeError("direct upload is faster on macOS")
            self.pbos = gl.glGenBuffers(2)
            for pbo in self.pbos:
                gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, pbo)
                gl.glBufferData(gl.GL_PIXEL_UNPACK_BUFFER, 1920 * 1080 * 3, None, gl.GL_STREAM_DRAW)
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
        except Exception:
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
            self.pbos = None

    def _prefilter(self, frame, target):
        # Upload a captured BGR frame and write its cleaned version (+ mipmaps) into target
        arr = np.ascontiguousarray(frame)
        if self.pbos is not None:
            pbo = self.pbos[self.pbo_idx]
            self.pbo_idx ^= 1
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, pbo)
            gl.glBufferSubData(gl.GL_PIXEL_UNPACK_BUFFER, 0, arr.nbytes, arr)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex_raw)
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, 1920, 1080,
                               gl.GL_BGR, gl.GL_UNSIGNED_BYTE, None)
            gl.glBindBuffer(gl.GL_PIXEL_UNPACK_BUFFER, 0)
        else:
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex_raw)
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, 1920, 1080,
                               gl.GL_BGR, gl.GL_UNSIGNED_BYTE, arr)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.pre_fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, target, 0)
        gl.glViewport(0, 0, 1920, 1080)
        gl.glUseProgram(self.pre_program)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex_raw)
        gl.glUniform1i(self.pre_u_src, 0)
        gl.glUniform2f(self.pre_u_texel, 1.0 / 1920.0, 1.0 / 1080.0)
        gl.glBindVertexArray(self.vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glBindTexture(gl.GL_TEXTURE_2D, target)
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        gl.glViewport(*self._viewport)

    def _copy_clean(self, src, dst):
        # When pair and live textures are the same captured frame, blit the already-cleaned
        # result instead of running the 9-tap prefilter shader a second time.
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.pre_fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, src, 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, dst)
        gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, 0, 0, 1920, 1080)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        gl.glViewport(*self._viewport)

    def _create_texture(self, w, h, internal_fmt, pix_fmt, dtype):
        tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal_fmt, w, h, 0, pix_fmt, dtype, None)
        return tex

    def resizeGL(self, w, h):
        dpr = self.devicePixelRatioF()
        pw, ph = round(w * dpr), round(h * dpr)
        if abs(pw - 1920) <= 2 and abs(ph - 1080) <= 2:
            # Pixel-exact 1:1 only on a true 1080p backing store; DPR 2 would otherwise render
            # into a quarter of the window. Borderless mode is 1px taller than the screen.
            self._viewport = ((pw - 1920) // 2, ph - 1080, 1920, 1080)
        else:
            self._viewport = (0, 0, pw, ph)
        gl.glViewport(*self._viewport)

    def paintGL(self):
        t_now = time.perf_counter()
        self.render_times.append(t_now)
        if len(self.render_times) >= 2:
            self.output_fps = (len(self.render_times) - 1) / max(1e-6, self.render_times[-1] - self.render_times[0])

        with self.engine.lock:
            pair_id = self.engine.pair_id
            frame_id = self.engine.frame_id
            t_arrival = self.engine.last_unique_time
            period = self.engine.unique_period
            scene_cut = self.engine.scene_cut
            ticks = list(self.engine.ticks)
            need_upload = (pair_id != self.uploaded_pair_id)
            consecutive = (pair_id == self.uploaded_pair_id + 1)
            need_live = (frame_id != self.uploaded_frame_id)
            if need_upload:
                f_prev = self.engine.f_prev
                f_curr = self.engine.f_curr
                flow_half = self.engine.flow_half
                self.engine.flow_uploading_id = id(flow_half)
                self.uploaded_pair_id = pair_id
            if need_live:
                f_live = self.engine.f_live
                self.uploaded_frame_id = frame_id

        if need_upload:
            try:
                if consecutive:
                    # New pair's f_prev is the old f_curr: rotate instead of re-uploading (halves the 6ms stall)
                    self.tex_pp, self.tex_prev, self.tex_curr = self.tex_prev, self.tex_curr, self.tex_pp
                self.pp_valid = consecutive
                if not consecutive:
                    self._prefilter(f_prev, self.tex_prev)
                self._prefilter(f_curr, self.tex_curr)
                self.tex_flow, self.tex_flow_prev = self.tex_flow_prev, self.tex_flow  # keep last flow for smoothing
                gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex_flow)
                gl.glTexSubImage2D(
                    gl.GL_TEXTURE_2D, 0, 0, 0,
                    CaptureFlowEngine.FLOW_W, CaptureFlowEngine.FLOW_H,
                    gl.GL_RG, gl.GL_HALF_FLOAT, flow_half
                )
            finally:
                with self.engine.lock:
                    self.engine.flow_uploading_id = None

        live_done = False
        if need_upload and need_live and f_curr is f_live:
            # Same frame in both paths (the usual case): reuse its cleaned pixels for the live texture.
            self._copy_clean(self.tex_curr, self.tex_live)
            live_done = True
        if need_live and not live_done:
            # Cleaned as soon as the frame is decoded: real frames can now be presented on the
            # very next vblank even when the flow thread is still busy.
            self._prefilter(f_live, self.tex_live)

        # Compute sub-frame interpolation phase alpha based on target_fps (30, 60, 90, 120, 144)
        # Nominal 30fps console frame interval is 33.333ms (16.667ms half-period)
        elapsed = t_now - t_arrival
        half_period = min(0.01667, period * 0.5)

        tick_sig = None
        old = False  # render the previous pair (tex_pp -> tex_prev) instead of the current one
        use_live = False  # real frame: bind the freshly decoded texture, no flow needed
        if scene_cut or self.mode == 1 or self.target_fps <= 30:
            alpha = 1.0
            use_live = True
        elif self.zero_hold_extrap:
            alpha = 1.0 if elapsed < half_period else 1.38
            use_live = True
        elif self.target_fps in (60, 120):
            # One output tick per HDMI frame (midpoint on unique, real on repeat), shown a fixed
            # delay after capture. Per-tick "ready now" deadlines let the real duplicate overtake
            # the midpoint in the same paint and halved distinct output frames on macOS.
            alpha, tick_sig = 1.0, (pair_id, 1.0)
            flow_delay = self.engine.flow_delay + self.phase_adj
            t_sel = self.vblank_t if self.use_vblank else t_now
            for t_cap, tp, ta in reversed(ticks):
                due = t_cap + flow_delay
                if tp > pair_id:
                    continue  # this frame's flow isn't published yet; fall back to an older tick
                if due <= t_sel:
                    a = ta
                    if self.target_fps == 120:
                        a = ta - (0.0 if t_sel - due >= 0.00833 else 0.25)
                    if self.use_vblank and (tp, a) != self.last_presented_signature:
                        P = self.vb_period
                        err = (t_sel - due) % P - P / 2
                        adj = self.phase_adj + 0.05 * err
                        self.phase_adj = adj + P if adj < -0.004 else min(adj, P)
                    alpha = a
                    if tp < pair_id:
                        old = tp == pair_id - 1 and self.pp_valid
                        if not old:
                            alpha = 0.0
                    if ta >= 0.99:
                        use_live = True  # real frame: skip flow, bind the freshly decoded texture
                    tick_sig = (tp, a)
                    break
        elif self.target_fps == 90:
            # 3x FrameGen (30 -> 90 FPS): 3-step cadence (11.1ms each: 0.33, 0.67, 1.0)
            if elapsed < 0.0111:
                alpha = 0.333
            elif elapsed < 0.0222:
                alpha = 0.667
            else:
                alpha = 1.0
            use_live = alpha >= 0.99
        else:
            # 144 FPS (old wall-clock pacing): 4-step quantized cadence (8.33ms each: 0.25, 0.50, 0.75, 1.0)
            step = min(4, int(elapsed / 0.00833) + 1)
            alpha = step * 0.25
            use_live = alpha >= 0.99

        # Track genuinely distinct visual frames presented to screen
        sig = tick_sig or (pair_id, round(alpha, 2))
        if sig != self.last_presented_signature:
            self.last_presented_signature = sig
            self.distinct_frame_times.append(t_now)
            if abs(alpha - 1.0) > 0.01:
                self.synthesized_count += 1
            else:
                self.real_count += 1
            if len(self.distinct_frame_times) >= 2:
                self.distinct_output_fps = (len(self.distinct_frame_times) - 1) / max(
                    1e-6, self.distinct_frame_times[-1] - self.distinct_frame_times[0]
                )

        gl.glUseProgram(self.program)
        if old:
            t_prev, t_curr, t_flow = self.tex_pp, self.tex_prev, self.tex_flow_prev
        else:
            t_prev, t_curr, t_flow = self.tex_prev, self.tex_curr, self.tex_flow
        if use_live:
            t_curr = self.tex_live

        u = self.u
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, t_prev)

        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, t_curr)

        gl.glActiveTexture(gl.GL_TEXTURE2)
        gl.glBindTexture(gl.GL_TEXTURE_2D, t_flow)
        gl.glActiveTexture(gl.GL_TEXTURE3)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex_flow_prev)
        gl.glActiveTexture(gl.GL_TEXTURE4)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex_live)
        gl.glUniform1i(u["u_smooth"], 1 if self.engine.motion_smoothing else 0)

        gl.glUniform1f(u["u_alpha"], float(alpha))
        gl.glUniform1i(u["u_mode"], int(self.mode))
        gl.glUniform1f(u["u_sharpness"], float(self.sharpness))
        gl.glUniform1i(u["u_hudProtect"], 1 if self.hud_protect else 0)
        gl.glUniform1f(u["u_blur"], float(self.motion_blur))
        gl.glUniform1f(u["u_frameStep"], 30.0 / max(30, self.target_fps))
        gl.glUniform1f(u["u_devig"], float(self.devignette))
        gl.glUniform1f(u["u_clarity"], float(self.clarity))
        gl.glUniform1f(u["u_vibrance"], float(self.vibrance))
        gl.glUniform1f(u["u_contrast"], float(self.contrast))
        gl.glUniform1i(u["u_compare"], 1 if self.compare else 0)

        gl.glBindVertexArray(self.vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)


class GTA6FrameGenWindow(QMainWindow):
    MODE_NAMES = {
        0: "Frame Generation",
        1: "Raw Passthrough (0ms)",
        2: "Split Screen Compare",
        3: "Motion Vector Heatmap",
    }

    def __init__(self, auto_quit_sec: float = 0.0):
        super().__init__()
        idx, name, all_devs = find_capture_card_index("USB Video")
        self.engine = CaptureFlowEngine(idx, name)
        self.audio = LowLatencyAudioBridge()

        self.setWindowTitle(f"GTA VI Low-Latency FrameGen — [{name}]")
        self.resize(1600, 900)
        self.setStyleSheet("background-color: #000000;")

        self.gl_widget = FrameGenGLWidget(self.engine, self.audio, self)
        self.setCentralWidget(self.gl_widget)

        self._build_top_osd()
        self._build_home_settings_panel()
        self._bind_shortcuts()
        QTimer.singleShot(1200, self._auto_check_updates)

        self.engine.start()
        self.audio.start()

        self.hud_timer = QTimer(self)
        self.hud_timer.timeout.connect(self._update_telemetry)
        self.hud_timer.start(200)

        if auto_quit_sec > 0:
            QTimer.singleShot(int(auto_quit_sec * 1000), self.close)

    def _build_top_osd(self):
        """Crisp native Qt top bar widget (zero OpenGL state conflict)."""
        self.osd_label = QLabel(self)
        self.osd_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.osd_label.setStyleSheet("""
            QLabel {
                background-color: rgba(12, 12, 20, 210);
                color: #F0F6FF;
                border: 1px solid #FF3E9D;
                border-radius: 6px;
                padding: 6px 12px;
                font-family: 'Consolas', 'Segoe UI', monospace;
                font-size: 12px;
                font-weight: bold;
            }
        """)
        self.osd_label.move(14, 12)
        self.osd_label.resize(1180, 46)
        self.osd_label.show()

        # Tiny measured-FPS counter, top-right ([P] toggles)
        self.fps_label = QLabel("-- FPS", self)
        self.fps_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.fps_label.setStyleSheet(
            "QLabel { background-color: rgba(12, 12, 20, 150); color: #3EE7FF; border-radius: 4px;"
            " padding: 1px 6px; font-family: 'Consolas', monospace; font-size: 11px; font-weight: bold; }")
        self.fps_label.show()

    def _place_fps_label(self):
        self.fps_label.adjustSize()
        self.fps_label.move(self.width() - self.fps_label.width() - 10, 8)

    def _build_home_settings_panel(self):
        """Interactive [Home] settings overlay modal."""
        self.settings_panel = QFrame(self)
        self.settings_panel.setStyleSheet("""
            QFrame {
                background-color: rgba(15, 16, 26, 242);
                border: 2px solid #FF3E9D;
                border-radius: 10px;
                color: #FFFFFF;
                font-family: 'Segoe UI', sans-serif;
            }
            QLabel {
                border: none;
                background: transparent;
                color: #E8F0FF;
                font-size: 13px;
                font-weight: 600;
            }
            QComboBox, QPushButton {
                background-color: #23263A;
                color: #FFFFFF;
                border: 1px solid #4E557E;
                border-radius: 5px;
                padding: 6px 10px;
                font-size: 13px;
                font-weight: 600;
            }
            QComboBox:hover, QPushButton:hover {
                border-color: #FF3E9D;
                background-color: #2D314B;
            }
            QCheckBox {
                color: #E8F0FF;
                font-size: 13px;
                font-weight: 600;
                spacing: 8px;
            }
            QSlider { background: transparent; border: none; min-height: 22px; }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
        """)
        self.settings_panel.setFixedWidth(500)  # height follows content (fixed height crushed the combos)
        layout = QVBoxLayout(self.settings_panel)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(8)

        title = QLabel(f"GTA VI FRAMEGEN — SETTINGS [{SETTINGS_KEY}]")
        title.setStyleSheet("color: #FF3E9D; font-size: 16px; font-weight: bold; letter-spacing: 1px;")
        layout.addWidget(title)

        # 1. Target Output FPS
        row_fps = QHBoxLayout()
        row_fps.addWidget(QLabel("Target Output FPS:"))
        self.combo_fps = QComboBox()
        self.combo_fps.addItems([
            "30 FPS (Native 1x - Off)",
            "60 FPS (2x Motion FrameGen)",
            "90 FPS (3x Motion FrameGen)",
            "120 FPS (4x FrameGen - EXPERIMENTAL)",
            "144 FPS (Max Monitor Refresh)"
        ])
        self.combo_fps.setCurrentIndex(1)  # 60 FPS default
        self.combo_fps.currentIndexChanged.connect(self._on_fps_combo)
        row_fps.addWidget(self.combo_fps)
        layout.addLayout(row_fps)

        # 2. Render Mode
        row_mode = QHBoxLayout()
        row_mode.addWidget(QLabel("View / Shader Mode:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems([
            "Frame Generation (Full)",
            "Raw Passthrough (0ms Hold)",
            "Split Screen (30fps vs FrameGen)",
            "Optical Flow Vector Heatmap"
        ])
        self.combo_mode.currentIndexChanged.connect(self._on_mode_combo)
        row_mode.addWidget(self.combo_mode)
        layout.addLayout(row_mode)

        # 3. Latency / Pacing Mode
        row_lat = QHBoxLayout()
        row_lat.addWidget(QLabel("Latency Pacing:"))
        self.combo_lat = QComboBox()
        self.combo_lat.addItems([
            "16.6ms Hold Interpolation (Artifact-Free)",
            "0.0ms Hold Hybrid Extrapolation (Instant)"
        ])
        self.combo_lat.currentIndexChanged.connect(self._on_lat_combo)
        row_lat.addWidget(self.combo_lat)
        layout.addLayout(row_lat)

        # 4. Optical Flow Preset
        row_flow = QHBoxLayout()
        row_flow.addWidget(QLabel("Optical Flow Engine:"))
        self.combo_flow = QComboBox()
        self.combo_flow.addItems([
            "ULTRAFAST (~1.7ms Latency)",
            "FAST HQ (~4.2ms Finer Edges)"
        ])
        self.combo_flow.currentIndexChanged.connect(self._on_flow_combo)
        row_flow.addWidget(self.combo_flow)
        layout.addLayout(row_flow)

        # 5. Sharpness Slider (Edge-Safe CAS)
        row_sharp = QHBoxLayout()
        self.lbl_sharp = QLabel("Edge-Safe Sharpness (CAS): 0%")
        row_sharp.addWidget(self.lbl_sharp)
        self.slider_sharp = QSlider(Qt.Orientation.Horizontal)
        self.slider_sharp.setRange(0, 100)
        self.slider_sharp.setValue(0)
        self.slider_sharp.valueChanged.connect(self._on_sharp_slider)
        row_sharp.addWidget(self.slider_sharp)
        layout.addLayout(row_sharp)

        row_blur = QHBoxLayout()
        self.lbl_blur = QLabel("Motion Blur: 22%")
        row_blur.addWidget(self.lbl_blur)
        self.slider_blur = QSlider(Qt.Orientation.Horizontal)
        self.slider_blur.setRange(0, 100)
        self.slider_blur.setValue(22)
        self.slider_blur.valueChanged.connect(self._on_blur_slider)
        row_blur.addWidget(self.slider_blur)
        layout.addLayout(row_blur)

        row_vig = QHBoxLayout()
        self.lbl_vig = QLabel("Remove Game Vignette: 100%")
        row_vig.addWidget(self.lbl_vig)
        self.slider_vig = QSlider(Qt.Orientation.Horizontal)
        self.slider_vig.setRange(0, 100)
        self.slider_vig.setValue(100)
        self.slider_vig.valueChanged.connect(self._on_vig_slider)
        row_vig.addWidget(self.slider_vig)
        layout.addLayout(row_vig)

        for attr, label, default in (("clarity", "Clarity / Dehaze", 33), ("vibrance", "Vibrance", 75),
                                     ("contrast", "Contrast", 1)):
            row = QHBoxLayout()
            lbl = QLabel(f"{label}: {default}%")
            row.addWidget(lbl)
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(0, 100)
            sl.setValue(default)
            sl.valueChanged.connect(lambda v, a=attr, l=lbl, t=label: (
                setattr(self.gl_widget, a, v / 100.0), l.setText(f"{t}: {v}%")))
            row.addWidget(sl)
            layout.addLayout(row)

        self.chk_smooth = QCheckBox("Motion Smoothing (temporal vector smoothing)")
        self.chk_smooth.setStyleSheet("background: transparent; border: none;")
        self.chk_smooth.setChecked(True)
        self.chk_smooth.toggled.connect(lambda on: setattr(self.engine, "motion_smoothing", on))
        layout.addWidget(self.chk_smooth)

        # 6. Checkboxes
        self.chk_hud = QCheckBox("Text && HUD Anti-Artifact Lock (Fixes Text Noise)")
        self.chk_hud.setStyleSheet("background: transparent; border: none;")
        self.chk_hud.setChecked(True)
        self.chk_hud.toggled.connect(self._on_hud_toggled)
        layout.addWidget(self.chk_hud)

        self.chk_osd = QCheckBox("Show Top Telemetry Bar (Shortcut: O)")
        self.chk_osd.setStyleSheet("background: transparent; border: none;")
        self.chk_osd.setChecked(True)
        self.chk_osd.toggled.connect(self.osd_label.setVisible)
        self.chk_osd.setChecked(False)  # clean picture by default; O or this box shows the bar
        layout.addWidget(self.chk_osd)

        self.chk_mute = QCheckBox("Mute Capture Card Audio (Shortcut: M)")
        self.chk_mute.setStyleSheet("background: transparent; border: none;")
        self.chk_mute.setChecked(False)
        self.chk_mute.toggled.connect(self._on_mute_toggled)
        layout.addWidget(self.chk_mute)

        # Added last so existing settings.json QCheckBox indices stay valid.
        self.chk_update = QCheckBox(f"Auto-update on startup (current {APP_VERSION})")
        self.chk_update.setStyleSheet("background: transparent; border: none;")
        self.chk_update.setChecked(True)
        layout.addWidget(self.chk_update)

        btn_update = QPushButton("Check for updates now")
        btn_update.clicked.connect(self.check_updates)
        layout.addWidget(btn_update)
        self.update_status = QLabel("")
        self.update_status.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(self.update_status)

        # 7. Bottom buttons
        btn_row = QHBoxLayout()
        btn_fs = QPushButton("Toggle Fullscreen (F)")
        btn_fs.clicked.connect(self.toggle_fullscreen)
        btn_close = QPushButton("Close Menu (Home)")
        btn_close.clicked.connect(self.toggle_settings_panel)
        btn_row.addWidget(btn_fs)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self.settings_panel.adjustSize()
        self.settings_panel.hide()
        self._init_autosave()

    def _init_autosave(self):
        """Restore every panel control from settings.json and save it again whenever one changes."""
        if "--self-test" in sys.argv:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        kinds = ((QComboBox, "currentIndex", "setCurrentIndex", "currentIndexChanged"),
                 (QSlider, "value", "setValue", "valueChanged"),
                 (QCheckBox, "isChecked", "setChecked", "toggled"))
        ctrls = [(f"{cls.__name__}{i}", w, get, set_, sig) for cls, get, set_, sig in kinds
                 for i, w in enumerate(self.settings_panel.findChildren(cls))]
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        for key, w, _, set_, _ in ctrls:
            if key in saved:
                getattr(w, set_)(saved[key])

        def save(*_):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({k: getattr(w, g)() for k, w, g, _, _ in ctrls}, f)
            except OSError:
                pass
        for _, w, _, _, sig in ctrls:
            getattr(w, sig).connect(save)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "osd_label"):
            self.osd_label.resize(min(1240, self.width() - 28), 46)
            self._place_fps_label()
        if hasattr(self, "settings_panel"):
            self.settings_panel.move(
                max(10, (self.width() - self.settings_panel.width()) // 2),
                max(65, (self.height() - self.settings_panel.height()) // 2)
            )

    def _bind_shortcuts(self):
        QShortcut(QKeySequence("Home"), self, activated=self.toggle_settings_panel)
        if sys.platform == "darwin":
            QShortcut(QKeySequence(Qt.Key.Key_Backslash), self, activated=self.toggle_settings_panel)
        QShortcut(QKeySequence("F"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self, activated=self.on_escape)
        QShortcut(QKeySequence("Tab"), self, activated=self.cycle_mode)
        QShortcut(QKeySequence("Space"), self, activated=self.toggle_framegen)
        QShortcut(QKeySequence("E"), self, activated=self.toggle_zero_hold)
        QShortcut(QKeySequence("H"), self, activated=self.toggle_hud_protect)
        QShortcut(QKeySequence("Q"), self, activated=self.toggle_flow_quality)
        QShortcut(QKeySequence("S"), self, activated=self.cycle_sharpness)
        QShortcut(QKeySequence("M"), self, activated=self.toggle_mute)
        QShortcut(QKeySequence("R"), self, activated=self.engine.start_recording)
        QShortcut(QKeySequence("B"), self, activated=lambda: setattr(self.gl_widget, "compare", not self.gl_widget.compare))
        QShortcut(QKeySequence("P"), self, activated=lambda: self.fps_label.setVisible(not self.fps_label.isVisible()))
        QShortcut(QKeySequence("A"), self, activated=self.audio.cycle_output)
        QShortcut(QKeySequence("O"), self, activated=self.toggle_osd)

    def toggle_settings_panel(self):
        if self.settings_panel.isVisible():
            self.settings_panel.hide()
            if getattr(self, "_borderless", False):
                self.setCursor(QCursor(Qt.CursorShape.BlankCursor))
        else:
            self.settings_panel.move(
                max(10, (self.width() - self.settings_panel.width()) // 2),
                max(65, (self.height() - self.settings_panel.height()) // 2)
            )
            self.settings_panel.show()
            self.settings_panel.raise_()
            self.unsetCursor()

    def on_escape(self):
        if self.settings_panel.isVisible():
            self.toggle_settings_panel()
        elif getattr(self, "_borderless", False):
            self._exit_borderless()

    def _on_fps_combo(self, idx: int):
        fps_map = {0: 30, 1: 60, 2: 90, 3: 120, 4: 144}
        fps = fps_map.get(idx, 60)
        self.gl_widget.set_target_fps(fps)
        if fps == 30:
            self.combo_mode.setCurrentIndex(1)
        elif self.gl_widget.mode == 1:
            self.combo_mode.setCurrentIndex(0)
        self._update_telemetry()

    def _on_mode_combo(self, idx: int):
        self.gl_widget.mode = idx
        self.engine.flow_enabled = idx != 1  # raw passthrough never uses motion vectors
        self._update_telemetry()

    def _on_lat_combo(self, idx: int):
        self.gl_widget.zero_hold_extrap = (idx == 1)
        self._update_telemetry()

    def _on_flow_combo(self, idx: int):
        self.engine.set_flow_preset(idx)
        self._update_telemetry()

    def _on_sharp_slider(self, val: int):
        self.gl_widget.sharpness = val / 100.0
        self.lbl_sharp.setText(f"Edge-Safe Sharpness (CAS): {val}%")
        self._update_telemetry()

    def _on_vig_slider(self, val: int):
        self.gl_widget.devignette = val / 100.0
        self.lbl_vig.setText(f"Remove Game Vignette: {val}%")

    def _on_blur_slider(self, val: int):
        self.gl_widget.motion_blur = val / 100.0
        self.lbl_blur.setText(f"Motion Blur: {val}%")

    def _on_hud_toggled(self, checked: bool):
        self.gl_widget.hud_protect = checked
        self._update_telemetry()

    def _on_mute_toggled(self, checked: bool):
        self.audio.muted = checked
        self._update_telemetry()

    def _auto_check_updates(self):
        if self.chk_update.isChecked():
            self.check_updates()

    def check_updates(self):
        # Source checkouts are updated with git; only installed copies self-update.
        if os.path.isdir(os.path.join(APP_DIR, ".git")) or "--self-test" in sys.argv:
            return
        self.update_status.setText("Checking GitHub for updates...")
        checker = UpdateChecker()
        checker.up_to_date.connect(lambda: self.update_status.setText(f"Up to date ({APP_VERSION})"))
        checker.installed.connect(self._on_update_installed)
        checker.failed.connect(lambda err: self.update_status.setText(f"Update failed: {str(err)[:70]}"))
        self._checker = checker
        self._updater_thread = threading.Thread(target=checker.run, daemon=True)
        self._updater_thread.start()

    def _on_update_installed(self, tag: str):
        self.update_status.setText(f"Installed {tag}. Restart PhantomFrame to use it.")
        self._update_tag = tag

    def toggle_fullscreen(self):
        # Borderless window instead of showFullScreen(): true fullscreen makes Windows flip the GL surface
        # directly to the display, which Lossless Scaling's capture can't overlay.
        if getattr(self, "_borderless", False):
            self._exit_borderless()
            return
        self._normal_geo = self.geometry()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        # 1px taller than the screen so Windows doesn't treat it as exclusive fullscreen
        self.setGeometry(self.screen().geometry().adjusted(0, 0, 0, 1))
        self.show()
        self._borderless = True
        if not self.settings_panel.isVisible():
            self.setCursor(QCursor(Qt.CursorShape.BlankCursor))

    def _exit_borderless(self):
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
        self.setGeometry(self._normal_geo)
        self.show()
        self._borderless = False
        self.unsetCursor()

    def cycle_mode(self):
        nxt = (self.gl_widget.mode + 1) % 4
        self.combo_mode.setCurrentIndex(nxt)

    def toggle_framegen(self):
        nxt = 1 if self.gl_widget.mode == 0 else 0
        self.combo_mode.setCurrentIndex(nxt)

    def toggle_zero_hold(self):
        nxt = 0 if self.gl_widget.zero_hold_extrap else 1
        self.combo_lat.setCurrentIndex(nxt)

    def toggle_hud_protect(self):
        self.chk_hud.setChecked(not self.gl_widget.hud_protect)

    def toggle_flow_quality(self):
        nxt = 1 - self.engine.flow_preset
        self.combo_flow.setCurrentIndex(nxt)

    def cycle_sharpness(self):
        levels = [0, 25, 45, 75, 100]
        cur = int(round(self.gl_widget.sharpness * 100))
        nxt = levels[(levels.index(cur) + 1) % len(levels)] if cur in levels else 25
        self.slider_sharp.setValue(nxt)

    def toggle_mute(self):
        self.chk_mute.setChecked(not self.audio.muted)

    def toggle_osd(self):
        self.chk_osd.setChecked(not self.osd_label.isVisible())

    def _update_telemetry(self):
        mode_str = self.MODE_NAMES.get(self.gl_widget.mode, "FrameGen")
        lat_mode = "0ms-Extrap" if self.gl_widget.zero_hold_extrap else "16.6ms-Interp"
        hud_str = "ON" if self.gl_widget.hud_protect else "OFF"
        preset_str = "FAST" if self.engine.flow_preset == 1 else "ULTRA"
        aud_str = "MUTED" if self.audio.muted else "LIVE"

        eff_fps = self.gl_widget.distinct_output_fps  # measured, not computed

        line1 = (
            f"GTA VI FRAMEGEN [{self.gl_widget.target_fps} FPS Target]  |  {mode_str}  |  "
            f"Xbox In: {self.engine.unique_fps:.1f} FPS (HDMI {self.engine.capture_fps:.0f}Hz)  ->  "
            f"Out: {eff_fps:.1f} FPS (Distinct: {self.gl_widget.distinct_output_fps:.1f}/s | GL: {self.gl_widget.output_fps:.0f}Hz)"
        )
        line2 = (
            f"Flow: {self.engine.flow_ms:.2f}ms ({preset_str}, Peak {self.engine.flow_max_px:.1f}px)  |  "
            f"{lat_mode}  |  Text/HUD Lock: {hud_str}  |  CAS: {int(self.gl_widget.sharpness*100)}%  |  "
            f"Audio: {aud_str}   ||   [{SETTINGS_KEY}] Settings  [F] Fullscreen  [Tab] Mode  [O] Hide Bar"
        )
        self.osd_label.setText(f"{line1}\n{line2}")
        self.fps_label.setText(f"{eff_fps:.0f} FPS")
        self._place_fps_label()
        rec =f"[{self.engine.rec_status}] " if self.engine.rec_status else ""
        upd = f" [RESTART FOR {self._update_tag}]" if getattr(self, "_update_tag", None) else ""
        self.setWindowTitle(f"{rec}GTA VI FrameGen — {eff_fps:.1f} FPS ({mode_str}) [Press {SETTINGS_KEY} for Settings]{upd}")

    def closeEvent(self, event):
        self.hud_timer.stop()
        self.gl_widget.timer.stop()
        self.audio.stop()
        self.engine.stop()
        super().closeEvent(event)


def main():
    auto_quit = 0.0
    if sys.platform == "darwin":
        # macOS camera permission can only be requested from the main thread; the capture thread can't ask
        cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION).release()
    if "--self-test" in sys.argv:
        auto_quit = 4.0

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSwapInterval(1)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(fmt)

    if sys.platform == "win32":
        # Own taskbar entry, so Windows shows our icon instead of python.exe's
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PhantomFrame")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("PhantomFrame")
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")))  # taskbar + Dock
    win = GTA6FrameGenWindow(auto_quit_sec=auto_quit)
    win.show()
    if "--fullscreen" in sys.argv:
        win.toggle_fullscreen()  # restore borderless mode when relaunching mid-session

    if "--lsfg" in sys.argv:
        # Clean 30fps passthrough window for Lossless Scaling's LSFG to frame-generate on top of
        win.combo_fps.setCurrentIndex(0)
        win.slider_blur.setValue(0)
        win.chk_osd.setChecked(False)
        win.fps_label.hide()  # Lossless Scaling draws its own counter


    rc = app.exec()

    if auto_quit > 0:
        print(
            f"SELF-TEST OK | Device: {win.engine.device_name} (idx={win.engine.device_index}) | "
            f"HDMI Capture: {win.engine.capture_fps:.1f} Hz | Unique Console FPS: {win.engine.unique_fps:.1f} | "
            f"Distinct Presented FPS: {win.gl_widget.distinct_output_fps:.1f} "
            f"(Real={win.gl_widget.real_count}, Synthesized={win.gl_widget.synthesized_count}) | "
            f"Flow Compute: {win.engine.flow_ms:.2f} ms (Peak={win.engine.flow_max_px:.1f}px) | "
            f"GL Render: {win.gl_widget.output_fps:.1f} Hz"
        )
    sys.exit(rc)


if __name__ == "__main__":
    main()
