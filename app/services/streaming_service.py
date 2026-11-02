"""HLS streaming: a supervised FFmpeg subprocess per stream.

FFmpeg reads the file/RTSP source, transcodes to H.264 and writes a rolling playlist of
short segments into a per-stream directory, which the API serves over plain HTTP.
"""

import collections
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.models.stream_models import Protocol, SourceType, StreamState
from app.services.base_stream import BaseStream, backoff_delays
from app.utils.video_utils import mask_url

log = logging.getLogger(__name__)

PLAYLIST = "index.m3u8"
STABLE_AFTER_SECONDS = 30  # a process that ran this long resets the backoff


class HlsStream(BaseStream):
    protocol = Protocol.HLS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.out_dir: Path = self.settings.hls_dir / self.id
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=10)
        self._thread = threading.Thread(target=self._run, name=f"ffmpeg-{self.id}", daemon=True)

    @property
    def playback_url(self) -> str:
        return f"/hls/{self.id}/{PLAYLIST}"

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._terminate()
        if self._thread.is_alive():
            self._thread.join(timeout)
        self.set_state(StreamState.STOPPED)
        shutil.rmtree(self.out_dir, ignore_errors=True)

    # -- ffmpeg ------------------------------------------------------------
    def _build_cmd(self) -> list[str]:
        s, req = self.settings, self.request
        cmd = [s.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-nostdin"]
        if self.source_type is SourceType.RTSP:
            cmd += ["-rtsp_transport", "tcp", "-timeout", str(int(s.rtsp_timeout_seconds * 1_000_000))]
        else:
            cmd += ["-re"]  # pace file reads at native speed so it behaves like a live feed
            if req.loop:
                cmd += ["-stream_loop", "-1"]
        # Live/looping streams never end; a one-shot file gets #EXT-X-ENDLIST so players stop polling.
        endless = self.source_type is SourceType.RTSP or req.loop
        flags = "delete_segments+independent_segments" + ("+omit_endlist" if endless else "")
        vf = f"fps={self.fps}"
        if req.width and req.height:
            vf += f",scale={req.width}:{req.height}"
        cmd += [
            "-i", str(self.resolved_source),
            "-an", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", str(self.fps * s.hls_segment_seconds), "-sc_threshold", "0",
            "-f", "hls",
            "-hls_time", str(s.hls_segment_seconds),
            "-hls_list_size", str(s.hls_list_size),
            "-hls_flags", flags,
            "-hls_segment_filename", str(self.out_dir / "seg_%05d.ts"),
            str(self.out_dir / PLAYLIST),
        ]  # fmt: skip
        return cmd

    def _terminate(self) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:  # ends when the process closes stderr
            line = mask_url(line.decode(errors="replace").strip())
            if line:
                self._stderr_tail.append(line)
                log.debug("ffmpeg[%s]: %s", self.id, line)

    # -- supervisor --------------------------------------------------------
    def _run(self) -> None:
        s = self.settings
        delays = backoff_delays(s.reconnect_initial_delay, s.reconnect_max_delay)
        is_file = self.source_type is SourceType.FILE
        attempts = 0
        while not self._stop.is_set():
            rc, ran_for = self._run_once()
            if self._stop.is_set():
                break
            reason = self._stderr_tail[-1] if self._stderr_tail else f"ffmpeg exited with code {rc}"
            if is_file:
                if rc == 0:
                    self.set_state(StreamState.ENDED)
                else:
                    self.set_state(StreamState.ERROR, reason)
                break
            if ran_for >= STABLE_AFTER_SECONDS:
                attempts, delays = 0, backoff_delays(s.reconnect_initial_delay, s.reconnect_max_delay)
            attempts += 1
            if s.max_reconnect_attempts and attempts > s.max_reconnect_attempts:
                self.set_state(StreamState.ERROR, f"{reason} (gave up after {attempts - 1} retries)")
                break
            self.reconnect_count += 1
            self.set_state(StreamState.RECONNECTING, reason)
            delay = next(delays)
            log.warning("Stream %s: %s; restarting ffmpeg in %.1fs", self.id, mask_url(reason), delay)
            self._stop.wait(delay)
        log.info("HLS stream %s supervisor exited (state=%s)", self.id, self.state.value)

    def _run_once(self) -> tuple[int | None, float]:
        playlist = self.out_dir / PLAYLIST
        playlist.unlink(missing_ok=True)
        self._stderr_tail.clear()
        started = time.monotonic()
        try:
            with self._proc_lock:
                if self._stop.is_set():
                    return None, 0.0
                self._proc = proc = subprocess.Popen(
                    self._build_cmd(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
        except OSError as exc:
            self._stderr_tail.append(f"Cannot launch ffmpeg: {exc.strerror or exc}")
            return -1, 0.0
        reader = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        reader.start()
        while proc.poll() is None and not self._stop.is_set():
            if self.state is not StreamState.RUNNING and playlist.exists():
                self.set_state(StreamState.RUNNING)
            self._stop.wait(0.3)
        rc = proc.poll()
        reader.join(2)
        return rc, time.monotonic() - started
