"""OpenCV capture worker + MJPEG frame distribution.

One daemon thread per stream reads frames incrementally (never the whole file), keeps only
the *latest* frame, and JPEG-encodes it once for all viewers. Slow viewers simply skip
frames, so memory stays bounded no matter how many clients connect.
"""

import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import Callable

import cv2

from app.models.stream_models import Protocol, SourceType, StreamState
from app.services.base_stream import BaseStream, backoff_delays
from app.utils.video_utils import mask_url

# Force TCP for RTSP (UDP drops packets on lossy networks and corrupts frames).
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

log = logging.getLogger(__name__)

CaptureFactory = Callable[["MjpegStream"], "cv2.VideoCapture"]


def default_capture_factory(stream: "MjpegStream") -> "cv2.VideoCapture":
    s = stream.settings
    if stream.source_type is SourceType.RTSP:
        ms = int(s.rtsp_timeout_seconds * 1000)
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms]
        return cv2.VideoCapture(str(stream.resolved_source), cv2.CAP_FFMPEG, params)
    if stream.source_type is SourceType.WEBCAM:
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(stream.resolved_source), backend)
        if stream.request.width and stream.request.height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, stream.request.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, stream.request.height)
        cap.set(cv2.CAP_PROP_FPS, stream.fps)
        return cap
    return cv2.VideoCapture(str(stream.resolved_source), cv2.CAP_FFMPEG)


class FrameBuffer:
    """Holds the latest frame; notifies asyncio viewers without a thread per viewer."""

    def __init__(self, jpeg_quality: int):
        self._lock = threading.Lock()
        self._quality = jpeg_quality
        self._frame = None
        self._jpeg: tuple[int, bytes] | None = None
        self.seq = 0
        self.closed = False
        self._viewers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        self._next_token = 0

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    def publish(self, frame) -> None:
        with self._lock:
            self._frame = frame
            self.seq += 1
        if self._viewers:
            self.jpeg()  # encode once in the worker thread, shared by all viewers
        self._notify()

    def jpeg(self) -> tuple[int, bytes] | None:
        """Return (seq, jpeg bytes) for the latest frame, encoding at most once per frame."""
        with self._lock:
            if self._frame is None:
                return None
            if self._jpeg and self._jpeg[0] == self.seq:
                return self._jpeg
            seq, frame = self.seq, self._frame
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._quality])
        if not ok:
            return None
        with self._lock:
            if seq >= (self._jpeg[0] if self._jpeg else 0):
                self._jpeg = (seq, buf.tobytes())
            return self._jpeg

    def peek_jpeg(self) -> tuple[int, bytes] | None:
        with self._lock:
            return self._jpeg if self._jpeg and self._jpeg[0] == self.seq else None

    def add_viewer(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> int:
        with self._lock:
            self._next_token += 1
            self._viewers[self._next_token] = (loop, event)
            return self._next_token

    def remove_viewer(self, token: int) -> None:
        with self._lock:
            self._viewers.pop(token, None)

    def close(self) -> None:
        self.closed = True
        self._notify()

    def _notify(self) -> None:
        with self._lock:
            targets = list(self._viewers.values())
        for loop, event in targets:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # viewer's loop already closed
                pass


class MjpegStream(BaseStream):
    protocol = Protocol.MJPEG

    def __init__(self, *args, capture_factory: CaptureFactory = default_capture_factory, **kwargs):
        super().__init__(*args, **kwargs)
        self.buffer = FrameBuffer(self.settings.jpeg_quality)
        self._factory = capture_factory
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"capture-{self.id}", daemon=True)

    @property
    def frames_published(self) -> int:  # type: ignore[override]
        return self.buffer.seq

    @property
    def viewers(self) -> int:  # type: ignore[override]
        return self.buffer.viewer_count

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("Capture thread for %s did not exit within %.0fs", self.id, timeout)
        self.set_state(StreamState.STOPPED)
        self.buffer.close()

    # -- worker ------------------------------------------------------------
    def _run(self) -> None:
        s = self.settings
        delays = backoff_delays(s.reconnect_initial_delay, s.reconnect_max_delay)
        attempts = 0
        is_file = self.source_type is SourceType.FILE
        opened_once = False
        log.info("Stream %s (%s) started", self.id, self.source_type.value)
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._factory(self)
                if not cap.isOpened():
                    raise ConnectionError("Unable to open video source")
                opened_once = True
                self.set_state(StreamState.RUNNING)
                outcome, frames = self._pump(cap)
                if frames:
                    attempts, delays = 0, backoff_delays(s.reconnect_initial_delay, s.reconnect_max_delay)
                if self._stop.is_set():
                    break
                if outcome == "eof":
                    if frames == 0:
                        raise ValueError("Video file contains no readable frames")
                    if self.request.loop:
                        continue
                    self.set_state(StreamState.ENDED)
                    break
                raise ConnectionError("Video source disconnected")
            except Exception as exc:  # noqa: BLE001 - worker must never die silently
                msg = mask_url(str(exc)) or exc.__class__.__name__
                # Files never retry; a webcam that has never opened is almost certainly a wrong index.
                # (An unplugged camera or a rebooting RTSP device is retried.)
                if is_file or (self.source_type is SourceType.WEBCAM and not opened_once):
                    if not is_file:
                        msg = f"Webcam {self.resolved_source} could not be opened (is the index right and the camera free?)"
                    log.error("Stream %s failed: %s", self.id, msg)
                    self.set_state(StreamState.ERROR, msg)
                    break
                attempts += 1
                if s.max_reconnect_attempts and attempts > s.max_reconnect_attempts:
                    log.error("Stream %s gave up after %d attempts: %s", self.id, attempts - 1, msg)
                    self.set_state(StreamState.ERROR, f"{msg} (gave up after {attempts - 1} retries)")
                    break
                self.reconnect_count += 1
                self.set_state(StreamState.RECONNECTING, msg)
                delay = next(delays)
                log.warning("Stream %s: %s; reconnecting in %.1fs (attempt %d)", self.id, msg, delay, attempts)
                self._stop.wait(delay)
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001
                        log.debug("release failed for %s", self.id, exc_info=True)
        if self.state in (StreamState.ENDED, StreamState.ERROR):
            self.buffer.close()  # lets connected viewers finish instead of waiting on a dead stream
        log.info("Stream %s worker exited (state=%s)", self.id, self.state.value)

    def _pump(self, cap) -> tuple[str, int]:
        """Read until EOF/disconnect/stop. Returns (outcome, frames_published)."""
        is_file = self.source_type is SourceType.FILE
        native = cap.get(cv2.CAP_PROP_FPS) or 0
        native_ok = is_file and 0 < native <= 240  # ignore bogus container values
        # A file can't play faster than it was recorded: cap at native fps, otherwise it would speed up.
        fps = min(self.fps, native) if native_ok else self.fps
        interval = 1.0 / fps
        skip = max(0, round(native / fps) - 1) if native_ok else 0
        req = self.request
        next_due = time.monotonic()
        published = bad_reads = 0
        while not self._stop.is_set():
            if not cap.grab():
                return ("eof" if is_file else "lost"), published
            now = time.monotonic()
            if not is_file and now < next_due:
                continue  # live source: keep draining the driver buffer, drop the frame
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                bad_reads += 1
                if bad_reads > 5:
                    return ("eof" if is_file else "lost"), published
                continue
            bad_reads = 0
            if req.width and req.height and (frame.shape[1], frame.shape[0]) != (req.width, req.height):
                frame = cv2.resize(frame, (req.width, req.height), interpolation=cv2.INTER_AREA)
            self.buffer.publish(frame)
            published += 1
            next_due += interval
            if next_due < now - 1.0:  # fell far behind; don't try to catch up in a burst
                next_due = now + interval
            if is_file:
                for _ in range(skip):
                    if not cap.grab():
                        break
                self._stop.wait(max(0.0, next_due - time.monotonic()))
            elif next_due < now:
                next_due = now
        return "stopped", published


async def mjpeg_frames(stream: MjpegStream, keepalive: float = 10.0):
    """Async generator yielding multipart MJPEG chunks for one viewer."""
    buf = stream.buffer
    event = asyncio.Event()
    token = buf.add_viewer(asyncio.get_running_loop(), event)
    last = 0
    try:
        while not buf.closed:
            event.clear()
            if buf.seq == last:
                try:
                    await asyncio.wait_for(event.wait(), keepalive)
                except asyncio.TimeoutError:
                    if stream.is_finished:
                        break
                    continue
                if buf.closed:
                    break
            item = buf.peek_jpeg() or await asyncio.to_thread(buf.jpeg)
            if item is None:
                continue
            last, data = item
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(data)).encode()
                + b"\r\n\r\n"
                + data
                + b"\r\n"
            )
    finally:
        buf.remove_viewer(token)
