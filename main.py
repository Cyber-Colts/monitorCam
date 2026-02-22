#!/usr/bin/env python3

import argparse
import glob
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from flask import Flask, Response, abort, render_template, stream_with_context

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


@dataclass
class CameraState:
    device: str
    index: int


class CameraWorker:
    def __init__(
        self,
        device: str,
        index: int,
        width: int,
        height: int,
        framerate: int,
        prefer_mjpeg: bool,
    ) -> None:
        self.device = device
        self.index = index
        self.width = width
        self.height = height
        self.framerate = framerate
        self.prefer_mjpeg = prefer_mjpeg
        self._framerate_options = self._build_framerate_options(framerate)
        self._current_framerate_index = 0
        self._lock = threading.Lock()
        self._latest_frame: Optional[bytes] = None
        self._frame_id = 0
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._process: Optional[subprocess.Popen] = None
        self._last_start = 0.0
        self._needs_backoff = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._terminate_process()
        self._thread.join(timeout=5)

    def update_index(self, index: int) -> None:
        self.index = index

    def get_frame(self) -> Tuple[Optional[bytes], int]:
        with self._lock:
            return self._latest_frame, self._frame_id

    def _terminate_process(self) -> None:
        proc = self._process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._process = None

    def _start_process(self) -> Optional[subprocess.Popen]:
        framerate = self._framerate_options[self._current_framerate_index]
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-framerate",
            str(framerate),
            "-video_size",
            f"{self.width}x{self.height}",
        ]
        if self.prefer_mjpeg:
            cmd.extend(["-input_format", "mjpeg"])
        cmd.extend([
            "-i",
            self.device,
            "-an",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "4",
            "-f",
            "mjpeg",
            "-",
        ])
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError:
            return None

    def _build_framerate_options(self, requested: int) -> List[int]:
        options = [requested, 60, 30, 24, 20, 15, 10, 5]
        seen = set()
        ordered: List[int] = []
        for value in options:
            if value <= 0:
                continue
            if value not in seen:
                seen.add(value)
                ordered.append(value)
        return ordered

    def _maybe_backoff_framerate(self) -> None:
        if self._current_framerate_index + 1 < len(self._framerate_options):
            self._current_framerate_index += 1

    def _run(self) -> None:
        buffer = b""
        last_frame_time = time.monotonic()
        while not self._stop_event.is_set():
            try:
                if not self._process or self._process.poll() is not None:
                    now = time.monotonic()
                    if self._needs_backoff or (self._last_start and now - self._last_start < 5):
                        self._maybe_backoff_framerate()
                        self._needs_backoff = False
                    self._process = self._start_process()
                    if not self._process:
                        time.sleep(2)
                        continue
                    self._last_start = time.monotonic()
                    last_frame_time = self._last_start
                    buffer = b""

                if not self._process.stdout:
                    time.sleep(0.2)
                    continue

                chunk = self._process.stdout.read(4096)
                if not chunk:
                    self._terminate_process()
                    time.sleep(0.2)
                    continue

                buffer += chunk
                while True:
                    start = buffer.find(JPEG_START)
                    if start == -1:
                        buffer = buffer[-len(JPEG_START):]
                        break
                    end = buffer.find(JPEG_END, start + 2)
                    if end == -1:
                        if start > 0:
                            buffer = buffer[start:]
                        break
                    frame = buffer[start : end + 2]
                    buffer = buffer[end + 2 :]
                    with self._lock:
                        self._latest_frame = frame
                        self._frame_id += 1
                    last_frame_time = time.monotonic()

                if time.monotonic() - last_frame_time > 5:
                    self._terminate_process()
                    self._needs_backoff = True
                    time.sleep(0.2)
            except Exception:
                self._terminate_process()
                self._needs_backoff = True
                time.sleep(0.5)

        self._terminate_process()


class CameraRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_index: Dict[int, CameraWorker] = {}

    def update(self, by_index: Dict[int, CameraWorker]) -> None:
        with self._lock:
            self._by_index = dict(by_index)

    def get(self, index: int) -> Optional[CameraWorker]:
        with self._lock:
            return self._by_index.get(index)

    def list_indices(self) -> List[int]:
        with self._lock:
            return sorted(self._by_index.keys())


def list_candidate_devices() -> List[str]:
    by_id = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    by_path = sorted(glob.glob("/dev/v4l/by-path/*-video-index0"))
    raw = by_id or by_path or sorted(glob.glob("/dev/video*"))

    devices: List[str] = []
    seen: set[str] = set()
    for path in raw:
        device = os.path.realpath(path)
        if not os.path.exists(device):
            continue
        if device in seen:
            continue
        seen.add(device)
        devices.append(device)
    return devices


def reconcile_workers(
    workers: Dict[str, CameraWorker],
    devices: List[str],
    width: int,
    height: int,
    framerate: int,
    prefer_mjpeg: bool,
) -> Dict[str, CameraWorker]:
    desired = set(devices)

    for device, worker in list(workers.items()):
        if device not in desired:
            worker.stop()
            workers.pop(device, None)

    for index, device in enumerate(devices):
        worker = workers.get(device)
        if not worker:
            worker = CameraWorker(device, index, width, height, framerate, prefer_mjpeg)
            worker.start()
            workers[device] = worker
        else:
            worker.update_index(index)

    return workers


def camera_manager_loop(
    args: argparse.Namespace,
    registry: CameraRegistry,
    stop_event: threading.Event,
) -> None:
    workers: Dict[str, CameraWorker] = {}

    while not stop_event.is_set():
        devices = list_candidate_devices()
        workers = reconcile_workers(
            workers,
            devices,
            args.width,
            args.height,
            args.framerate,
            args.prefer_mjpeg,
        )
        registry.update({worker.index: worker for worker in workers.values()})
        stop_event.wait(args.rescan_seconds)

    for worker in list(workers.values()):
        worker.stop()


def create_app(registry: CameraRegistry) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return render_template("index.html", indices=registry.list_indices())

    @app.get("/cam/<int:index>/")
    def cam_view(index: int) -> str:
        worker = registry.get(index)
        if not worker:
            abort(404)
        return (
            "<html><body style='margin:0'>"
            f"<img src='/cam/{index}/stream.mjpg' style='width:100%;height:auto' />"
            "</body></html>"
        )

    @app.get("/cam/<int:index>/snapshot.jpg")
    def snapshot(index: int) -> Response:
        worker = registry.get(index)
        if not worker:
            abort(404)
        frame, _frame_id = worker.get_frame()
        if not frame:
            abort(503)
        return Response(frame, mimetype="image/jpeg")

    @app.get("/cam/<int:index>/stream.mjpg")
    def stream(index: int) -> Response:
        worker = registry.get(index)
        if not worker:
            abort(404)

        def generate():
            last_id = -1
            while True:
                frame, frame_id = worker.get_frame()
                if not frame:
                    time.sleep(0.05)
                    continue
                if frame_id == last_id:
                    time.sleep(0.01)
                    continue
                last_id = frame_id
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )

        return Response(
            stream_with_context(generate()),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream cameras via Flask using ffmpeg.")
    parser.add_argument("--http-host", default="0.0.0.0", help="Flask bind address.")
    parser.add_argument("--http-port", type=int, default=80, help="Flask bind port.")
    parser.add_argument("--width", type=int, default=640, help="Capture width.")
    parser.add_argument("--height", type=int, default=480, help="Capture height.")
    parser.add_argument("--framerate", type=int, default=60, help="Requested framerate.")
    parser.add_argument(
        "--prefer-mjpeg",
        action="store_true",
        help="Prefer MJPEG input to reduce USB bandwidth when available.",
    )
    parser.add_argument("--rescan-seconds", type=int, default=10, help="Device rescan interval.")
    args = parser.parse_args()

    registry = CameraRegistry()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=camera_manager_loop,
        args=(args, registry, stop_event),
        daemon=True,
    )
    thread.start()

    def shutdown(_signum: int, _frame) -> None:
        stop_event.set()
        thread.join(timeout=5)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    app = create_app(registry)
    app.run(host=args.http_host, port=args.http_port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
