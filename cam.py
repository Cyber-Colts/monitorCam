#!/usr/bin/env python3
"""
FRC-optimized multi-camera MJPEG streaming server for Raspberry Pi 4.

Architecture
============
Capture modes (--mode):

  passthrough  [DEFAULT, RECOMMENDED]
               USB camera MJPEG bytes are piped through unchanged.
               ffmpeg uses -vcodec copy — zero CPU re-encoding.
               Latency is limited only by USB frame delivery (~5-15 ms).

  hw           Pi 4 VPU (VideoCore VI) via ffmpeg h264_v4l2m2m encodes
               raw sensor frames to H.264 on the hardware encoder.
               Use when the camera does NOT support native MJPEG, or
               when you need GPU-side processing hooks.

  sw           Software MJPEG fallback. Avoid on Pi 4 — maxes CPU.

FRC port convention: 5800-5810.  Default: 5800.

Install
=======
  pip install flask waitress

Run
===
  sudo python3 camera_server.py                   # passthrough, port 544
  sudo python3 camera_server.py --mode hw         # Pi 4 VPU H.264 path
  sudo python3 camera_server.py --port 5801 --framerate 30 --width 640 --height 480
"""

import argparse
import fcntl
import glob
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from flask import Flask, Response, abort, render_template_string, stream_with_context
from waitress import serve

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("frc-cam")

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
JPEG_SOI            = b"\xff\xd8"
JPEG_EOI            = b"\xff\xd9"
PIPE_BUFFER_BYTES   = 1 << 20       # 1 MiB kernel pipe buffer (requires root)
READ_CHUNK          = 131_072       # 128 KiB — fewer syscalls per second
RESTART_DELAY       = 0.15          # seconds to wait before re-spawning ffmpeg
STALL_TIMEOUT       = 4.0           # seconds of no frames before restart
FRAME_POLL_SLEEP    = 0.001         # 1 ms generator poll — keeps latency <2 ms
MANAGER_RESCAN      = 8             # seconds between device rescan

# Pi 4 VPU M2M encoder node
V4L2_M2M_DEVICE     = "/dev/video11"

# ---------------------------------------------------------------------------
# ffmpeg command builders
# ---------------------------------------------------------------------------

def _base_input_args(
    device: str, fmt: str, width: int, height: int, framerate: int
) -> List[str]:
    """Common low-latency V4L2 input arguments."""
    return [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        # Eliminate all buffering / probing delay
        "-fflags",            "nobuffer+discardcorrupt",
        "-flags",             "low_delay",
        "-avioflags",         "direct",
        "-probesize",         "32",
        "-analyzeduration",   "0",
        "-thread_queue_size", "4",
        # V4L2 capture
        "-f",            "v4l2",
        "-input_format", fmt,
        "-framerate",    str(framerate),
        "-video_size",   f"{width}x{height}",
        "-i",            device,
        "-an",
    ]


def build_passthrough_cmd(
    device: str, width: int, height: int, framerate: int
) -> List[str]:
    """
    Copy native MJPEG bytes from sensor to stdout.
    Zero CPU encode cost — requires camera to support MJPEG output
    (virtually all USB webcams used in FRC do).
    """
    return _base_input_args(device, "mjpeg", width, height, framerate) + [
        "-vcodec",        "copy",
        "-f",             "mjpeg",
        "-flush_packets", "1",
        "-",
    ]


def build_hw_cmd(
    device: str, width: int, height: int, framerate: int, bitrate: str
) -> List[str]:
    """
    Pi 4 VPU H.264 encoding via h264_v4l2m2m.
    Input is raw yuyv422 (sensor native) because we are not piggybacking
    on the camera MJPEG path.  Output is a raw H.264 bytestream.
    """
    return _base_input_args(device, "yuyv422", width, height, framerate) + [
        "-c:v",           "h264_v4l2m2m",
        "-device",        V4L2_M2M_DEVICE,
        "-b:v",           bitrate,
        "-g",             "1",    # every frame is a keyframe — lowest latency
        "-bf",            "0",    # no B-frames
        "-f",             "h264",
        "-flush_packets", "1",
        "-",
    ]


def build_sw_cmd(
    device: str, width: int, height: int, framerate: int
) -> List[str]:
    """Software MJPEG fallback — uses CPU ARMv8 NEON path. Avoid if possible."""
    return _base_input_args(device, "mjpeg", width, height, framerate) + [
        "-vcodec",        "mjpeg",
        "-q:v",           "5",
        "-f",             "mjpeg",
        "-flush_packets", "1",
        "-",
    ]


def _spawn(cmd: List[str]) -> Optional[subprocess.Popen]:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        # Increase kernel pipe buffer to reduce back-pressure on ffmpeg.
        # F_SETPIPE_SZ = 1031 (Linux).  Silently skip if unprivileged.
        try:
            fcntl.fcntl(proc.stdout.fileno(), 1031, PIPE_BUFFER_BYTES)
        except OSError:
            pass
        return proc
    except (FileNotFoundError, OSError) as exc:
        log.error("Failed to spawn ffmpeg: %s", exc)
        return None

# ---------------------------------------------------------------------------
# H.264 NAL splitter (hw mode only)
# ---------------------------------------------------------------------------

H264_STARTCODE = b"\x00\x00\x00\x01"


def split_h264_nalus(buf: bytes) -> tuple[list[bytes], bytes]:
    nalus: list[bytes] = []
    last = 0
    i    = 0
    while True:
        idx = buf.find(H264_STARTCODE, i + 4)
        if idx == -1:
            break
        nalus.append(buf[last:idx])
        last = idx
        i    = idx
    return nalus, buf[last:]

# ---------------------------------------------------------------------------
# Camera worker
# ---------------------------------------------------------------------------

@dataclass
class CameraWorker:
    device:      str
    index:       int
    width:       int
    height:      int
    framerate:   int
    mode:        str    # "passthrough" | "hw" | "sw"
    hw_bitrate:  str

    _lock:       threading.Lock           = field(default_factory=threading.Lock,  init=False, repr=False)
    _frame:      Optional[bytes]          = field(default=None,                    init=False, repr=False)
    _frame_id:   int                      = field(default=0,                       init=False, repr=False)
    _stop_event: threading.Event          = field(default_factory=threading.Event, init=False, repr=False)
    _process:    Optional[subprocess.Popen] = field(default=None,                 init=False, repr=False)
    _thread:     Optional[threading.Thread] = field(default=None,                 init=False, repr=False)

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"cam{self.index}-{os.path.basename(self.device)}",
        )
        self._thread.start()
        log.info(
            "[cam%d] started  device=%s  mode=%s  %dx%d@%dfps",
            self.index, self.device, self.mode,
            self.width, self.height, self.framerate,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=5)

    def update_index(self, idx: int) -> None:
        self.index = idx

    def get_frame(self) -> tuple[Optional[bytes], int]:
        with self._lock:
            return self._frame, self._frame_id

    # ------------------------------------------------------------------
    def _kill(self) -> None:
        proc, self._process = self._process, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _spawn_proc(self) -> Optional[subprocess.Popen]:
        if self.mode == "passthrough":
            cmd = build_passthrough_cmd(self.device, self.width, self.height, self.framerate)
        elif self.mode == "hw":
            cmd = build_hw_cmd(self.device, self.width, self.height, self.framerate, self.hw_bitrate)
        else:
            cmd = build_sw_cmd(self.device, self.width, self.height, self.framerate)
        return _spawn(cmd)

    def _run(self) -> None:
        if self.mode == "hw":
            self._run_h264()
        else:
            self._run_mjpeg()

    # ------------------------------------------------------------------
    # Mode A: MJPEG passthrough / sw re-encode
    # ------------------------------------------------------------------
    def _run_mjpeg(self) -> None:
        buf            = b""
        last_frame_t   = time.monotonic()

        while not self._stop_event.is_set():
            # Restart ffmpeg if not running
            if not self._process or self._process.poll() is not None:
                self._kill()
                self._process = self._spawn_proc()
                if not self._process:
                    time.sleep(RESTART_DELAY)
                    continue
                buf          = b""
                last_frame_t = time.monotonic()

            # Read a large chunk (reduces syscall frequency)
            try:
                chunk = self._process.stdout.read(READ_CHUNK)
            except Exception:
                self._kill()
                continue

            if not chunk:
                self._kill()
                time.sleep(RESTART_DELAY)
                continue

            buf += chunk

            # Extract all complete JPEG frames
            while True:
                start = buf.find(JPEG_SOI)
                if start == -1:
                    buf = b""
                    break

                end = buf.find(JPEG_EOI, start + 2)
                if end == -1:
                    buf = buf[start:]   # Preserve incomplete frame
                    break

                frame = buf[start : end + 2]
                buf   = buf[end + 2:]

                with self._lock:
                    self._frame    = frame
                    self._frame_id += 1

                last_frame_t = time.monotonic()

            # Stall watchdog
            if time.monotonic() - last_frame_t > STALL_TIMEOUT:
                log.warning("[cam%d] stalled — restarting ffmpeg", self.index)
                self._kill()
                time.sleep(RESTART_DELAY)

        self._kill()

    # ------------------------------------------------------------------
    # Mode B: Pi 4 VPU H.264 TODO: ADD H.265 SUPPORT!
    # ------------------------------------------------------------------
    def _run_h264(self) -> None:
        remainder    = b""
        last_frame_t = time.monotonic()

        while not self._stop_event.is_set():
            if not self._process or self._process.poll() is not None:
                self._kill()
                self._process = self._spawn_proc()
                if not self._process:
                    time.sleep(RESTART_DELAY)
                    continue
                remainder    = b""
                last_frame_t = time.monotonic()

            try:
                chunk = self._process.stdout.read(READ_CHUNK)
            except Exception:
                self._kill()
                continue

            if not chunk:
                self._kill()
                time.sleep(RESTART_DELAY)
                continue

            buf               = remainder + chunk
            nalus, remainder  = split_h264_nalus(buf)

            for nalu in nalus:
                if not nalu:
                    continue
                with self._lock:
                    self._frame    = nalu
                    self._frame_id += 1
                last_frame_t = time.monotonic()

            if time.monotonic() - last_frame_t > STALL_TIMEOUT:
                log.warning("[cam%d] H.264 stalled — restarting", self.index)
                self._kill()
                time.sleep(RESTART_DELAY)

        self._kill()

# ---------------------------------------------------------------------------
# Camera registry
# ---------------------------------------------------------------------------

class CameraRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cams: Dict[int, CameraWorker] = {}

    def update(self, cams: Dict[int, CameraWorker]) -> None:
        with self._lock:
            self._cams = dict(cams)

    def get(self, index: int) -> Optional[CameraWorker]:
        with self._lock:
            return self._cams.get(index)

    def list_indices(self) -> List[int]:
        with self._lock:
            return sorted(self._cams.keys())

# ---------------------------------------------------------------------------
# Device discovery ( borring shit....)
# ---------------------------------------------------------------------------

def discover_devices() -> List[str]:
    """
    Return real /dev/videoN paths in stable plug-order.
    Prefers /dev/v4l/by-id symlinks so camera indices do not shuffle
    on reboot or re-plug — critical for consistent FRC driver mapping.
    """
    candidates = (
        sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
        or sorted(glob.glob("/dev/v4l/by-path/*-video-index0"))
        or sorted(glob.glob("/dev/video[0-9]*"))
    )
    seen:    set[str]  = set()
    devices: List[str] = []
    for path in candidates:
        real = os.path.realpath(path)
        if os.path.exists(real) and real not in seen:
            seen.add(real)
            devices.append(real)
    return devices

# ---------------------------------------------------------------------------
# Manager loop
# ---------------------------------------------------------------------------

def manager_loop(
    args:       argparse.Namespace,
    registry:   CameraRegistry,
    stop_event: threading.Event,
) -> None:
    workers: Dict[str, CameraWorker] = {}

    while not stop_event.is_set():
        devices = discover_devices()
        desired = set(devices)

        for dev in list(workers):
            if dev not in desired:
                log.info("Device removed: %s", dev)
                workers[dev].stop()
                del workers[dev]

        for i, dev in enumerate(devices):
            if dev not in workers:
                w = CameraWorker(
                    device=dev, index=i,
                    width=args.width, height=args.height,
                    framerate=args.framerate,
                    mode=args.mode,
                    hw_bitrate=args.hw_bitrate,
                )
                w.start()
                workers[dev] = w
            else:
                workers[dev].update_index(i)

        registry.update({w.index: w for w in workers.values()})
        stop_event.wait(MANAGER_RESCAN)

    for w in workers.values():
        w.stop()

# ---------------------------------------------------------------------------
# pretty html :)
# ---------------------------------------------------------------------------

INDEX_TMPL = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>FRC Camera Hub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;700&family=Share+Tech+Mono&display=swap" rel="stylesheet"/>
  <style>
    :root{--green:#00ff9f;--red:#ff3b3b;--bg:#0a0e13;--surface:#111820;
          --border:#1e2d3d;--dim:#4a6070}
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:var(--bg);color:#c9d8e8;font-family:'Rajdhani',sans-serif;min-height:100vh;padding:16px}
    header{display:flex;align-items:center;gap:14px;margin-bottom:20px;
           border-bottom:1px solid var(--border);padding-bottom:12px}
    .logo{font-size:1.5rem;font-weight:700;letter-spacing:.1em;color:var(--green);text-transform:uppercase}
    .badge{font-family:'Share Tech Mono',monospace;font-size:.7rem;padding:2px 8px;
           border:1px solid var(--green);color:var(--green);border-radius:2px;letter-spacing:.1em}
    .badge.dim{border-color:var(--dim);color:var(--dim)}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px}
    .cam-card{background:var(--surface);border:1px solid var(--border);border-radius:4px;overflow:hidden}
    .cam-header{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;
                border-bottom:1px solid var(--border);background:rgba(0,255,159,.03)}
    .cam-title{font-size:1rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
    .cam-links{display:flex;gap:8px}
    .cam-links a{font-family:'Share Tech Mono',monospace;font-size:.68rem;color:var(--dim);
                 text-decoration:none;padding:3px 8px;border:1px solid var(--border);
                 border-radius:2px;transition:color .15s,border-color .15s}
    .cam-links a:hover{color:var(--green);border-color:var(--green)}
    .cam-img-wrap{position:relative;background:#000;aspect-ratio:4/3}
    .cam-img-wrap img{width:100%;height:100%;object-fit:contain;display:block}
    .cam-footer{display:flex;align-items:center;justify-content:space-between;padding:5px 14px;
                font-family:'Share Tech Mono',monospace;font-size:.65rem;color:var(--dim);
                border-top:1px solid var(--border)}
    .live-dot{width:7px;height:7px;border-radius:50%;background:#333;display:inline-block;
              margin-right:5px;transition:background .3s}
    .live-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
    .empty{text-align:center;padding:80px 20px;color:var(--dim);font-family:'Share Tech Mono',monospace;
           font-size:.85rem;grid-column:1/-1;border:1px dashed var(--border);border-radius:4px}
  </style>
</head>
<body>
  <header>
    <div class="logo">FRC Camera Hub</div>
    <div class="badge">{{ cam_count }} CAM{% if cam_count != 1 %}S{% endif %}</div>
    <div class="badge dim">{{ mode.upper() }} MODE</div>
  </header>
  <div class="grid">
    {% if indices %}
      {% for idx in indices %}
      <div class="cam-card">
        <div class="cam-header">
          <span class="cam-title">Camera {{ idx }}</span>
          <div class="cam-links">
            <a href="/cam/{{ idx }}/">Full</a>
            <a href="/cam/{{ idx }}/snapshot.jpg" download>Snap</a>
          </div>
        </div>
        <div class="cam-img-wrap">
          <img id="img{{ idx }}" src="/cam/{{ idx }}/stream.mjpg"
               alt="cam {{ idx }}" onerror="onErr({{ idx }})"/>
        </div>
        <div class="cam-footer">
          <span><span class="live-dot" id="dot{{ idx }}"></span>
                <span id="lbl{{ idx }}">connecting…</span></span>
          <span id="fps{{ idx }}">—</span>
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">NO CAMERAS DETECTED<br><br>
        Plug in a USB camera — auto-detected in ~{{ rescan }}s.</div>
    {% endif %}
  </div>
<script>
const indices = {{ indices | tojson }};
indices.forEach(i => {
  const img = document.getElementById('img' + i);
  const dot = document.getElementById('dot' + i);
  const lbl = document.getElementById('lbl' + i);
  const fps = document.getElementById('fps' + i);
  let n = 0, last = performance.now();
  img.addEventListener('load', () => {
    n++;
    dot.classList.add('on');
    const now = performance.now();
    if (now - last >= 1000) {
      fps.textContent = (n / ((now - last) / 1000)).toFixed(1) + ' fps';
      n = 0; last = now;
    }
    lbl.textContent = 'live';
  });
  img.addEventListener('error', () => {
    dot.classList.remove('on');
    lbl.textContent = 'retrying…';
    setTimeout(() => { img.src = '/cam/' + i + '/stream.mjpg?' + Date.now(); }, 2000);
  });
});
function onErr(i) {}
</script>
</body>
</html>"""

CAM_TMPL = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Cam {{ index }}</title>
  <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@700&family=Share+Tech+Mono&display=swap" rel="stylesheet"/>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#000;display:flex;flex-direction:column;height:100dvh}
    header{display:flex;align-items:center;padding:8px 14px;gap:12px;
           background:rgba(255,255,255,.05);flex-shrink:0}
    header a{color:#4a6070;font-size:.8rem;text-decoration:none;font-family:'Rajdhani',sans-serif}
    header a:hover{color:#00ff9f}
    h1{color:#c9d8e8;font-size:.95rem;font-family:'Rajdhani',sans-serif;
       letter-spacing:.1em;text-transform:uppercase;flex:1}
    .pill{font-family:'Share Tech Mono',monospace;font-size:.65rem;padding:2px 10px;
          border-radius:999px;background:#111820;color:#4a6070;border:1px solid #1e2d3d}
    .wrap{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden}
    img{max-width:100%;max-height:100%;object-fit:contain;display:block}
  </style>
</head>
<body>
  <header>
    <a href="/">← Back</a>
    <h1>Camera {{ index }}</h1>
    <div class="pill" id="pill">connecting…</div>
  </header>
  <div class="wrap">
    <img id="stream" src="/cam/{{ index }}/stream.mjpg" alt="cam {{ index }}"/>
  </div>
<script>
const img = document.getElementById('stream');
const pill = document.getElementById('pill');
let n = 0, last = performance.now();
img.addEventListener('load', () => {
  n++;
  const now = performance.now();
  if (now - last >= 1000) {
    pill.textContent = 'LIVE  ' + (n / ((now - last) / 1000)).toFixed(1) + ' fps';
    pill.style.color = '#00ff9f'; pill.style.borderColor = '#00ff9f';
    n = 0; last = now;
  }
});
img.addEventListener('error', () => {
  pill.textContent = 'reconnecting…';
  pill.style.color = '#ff3b3b'; pill.style.borderColor = '#ff3b3b';
  setTimeout(() => { img.src = '/cam/{{ index }}/stream.mjpg?' + Date.now(); }, 1500);
});
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

def create_app(registry: CameraRegistry, mode: str, rescan: int) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        indices = registry.list_indices()
        return render_template_string(
            INDEX_TMPL,
            indices=indices,
            cam_count=len(indices),
            mode=mode,
            rescan=rescan,
        )

    @app.get("/cam/<int:idx>/")
    def cam_view(idx: int):
        if not registry.get(idx):
            abort(404)
        return render_template_string(CAM_TMPL, index=idx)

    @app.get("/cam/<int:idx>/snapshot.jpg")
    def snapshot(idx: int):
        worker = registry.get(idx)
        if not worker:
            abort(404)
        frame, _ = worker.get_frame()
        if not frame:
            abort(503)
        return Response(
            frame, mimetype="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="cam{idx}.jpg"',
            },
        )

    @app.get("/cam/<int:idx>/stream.mjpg")
    def stream(idx: int):
        worker = registry.get(idx)
        if not worker:
            abort(404)

        def generate() -> Iterator[bytes]:
            last_id = -1
            while True:
                frame, fid = worker.get_frame()
                if not frame or fid == last_id:
                    time.sleep(FRAME_POLL_SLEEP)
                    continue
                last_id = fid
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                    + frame
                    + b"\r\n"
                )

        return Response(
            stream_with_context(generate()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control":     "no-store, no-cache, must-revalidate",
                "Pragma":            "no-cache",
                "X-Accel-Buffering": "no",   # Disable nginx buffering if proxied
            },
        )

    @app.get("/health")
    def health():
        """Health check for FRC dashboard / robot code connectivity polling."""
        return {"status": "ok", "cameras": registry.list_indices()}

    return app

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="FRC Pi 4 multi-camera MJPEG server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host",       default="0.0.0.0")
    parser.add_argument("--port",       type=int,  default=544,
                        help="FRC convention: 5800-5810")
    parser.add_argument("--width",      type=int,  default=640)
    parser.add_argument("--height",     type=int,  default=480)
    parser.add_argument("--framerate",  type=int,  default=30)
    parser.add_argument(
        "--mode",
        choices=["passthrough", "hw", "sw"],
        default="passthrough",
        help=(
            "passthrough: vcodec copy, zero CPU, recommended. "
            "hw: Pi 4 VPU h264_v4l2m2m. "
            "sw: CPU MJPEG fallback."
        ),
    )
    parser.add_argument("--hw-bitrate", default="4M",
                        help="H.264 target bitrate (hw mode only)")
    parser.add_argument("--rescan",     type=int,  default=MANAGER_RESCAN,
                        help="Camera rescan interval seconds")
    parser.add_argument("--threads",    type=int,  default=8,
                        help="Waitress worker threads")
    args = parser.parse_args()

    # Raise process priority — needs root; silently skip otherwise
    try:
        os.nice(-10)
        log.info("Process priority raised (nice=-10)")
    except OSError:
        log.warning("Could not raise process priority (run with sudo for lowest latency)")

    registry   = CameraRegistry()
    stop_event = threading.Event()

    manager_thread = threading.Thread(
        target=manager_loop,
        args=(args, registry, stop_event),
        daemon=True, name="manager",
    )
    manager_thread.start()

    def shutdown(*_) -> None:
        log.info("Shutting down…")
        stop_event.set()
        manager_thread.join(timeout=8)
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    app = create_app(registry, args.mode, args.rescan)

    log.info(
        "FRC Camera Server | mode=%s | %dx%d@%dfps | http://%s:%d",
        args.mode, args.width, args.height, args.framerate, args.host, args.port,
    )

    # Waitress: production WSGI — no debug overhead, handles many concurrent streams
    serve(
        app,
        host=args.host,
        port=args.port,
        threads=args.threads,
        channel_timeout=60,
        cleanup_interval=10,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
