"""Registry and lifecycle management for all active streams."""

import asyncio
import logging
import re
import shutil
import threading
import uuid

from app.core.config import Settings
from app.core.errors import InvalidSource, ProtocolUnavailable, StreamLimitReached, StreamNotFound
from app.models.stream_models import Protocol, SourceType, StartStreamRequest, StreamInfo
from app.services.base_stream import BaseStream
from app.services.streaming_service import HlsStream
from app.services.video_capture import CaptureFactory, MjpegStream, default_capture_factory
from app.utils.video_utils import resolve_media_file

log = logging.getLogger(__name__)
_STREAM_ID_DIR = re.compile(r"[0-9a-f]{12}")


class StreamManager:
    def __init__(self, settings: Settings, capture_factory: CaptureFactory = default_capture_factory):
        self.settings = settings
        self._capture_factory = capture_factory
        self._streams: dict[str, BaseStream] = {}
        self._lock = threading.Lock()
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        settings.hls_dir.mkdir(parents=True, exist_ok=True)
        self._remove_stale_hls_dirs()

    def _remove_stale_hls_dirs(self) -> None:
        """Delete segment dirs left by a crashed run. Only touches dirs named like our stream ids."""
        for child in self.settings.hls_dir.iterdir():
            if child.is_dir() and _STREAM_ID_DIR.fullmatch(child.name):
                shutil.rmtree(child, ignore_errors=True)

    # -- lookup ------------------------------------------------------------
    def get(self, stream_id: str) -> BaseStream:
        with self._lock:
            stream = self._streams.get(stream_id)
        if stream is None:
            raise StreamNotFound(f"Stream '{stream_id}' not found")
        return stream

    def list(self) -> list[StreamInfo]:
        with self._lock:
            streams = list(self._streams.values())
        return [s.info() for s in streams]

    # -- lifecycle ---------------------------------------------------------
    def start(self, req: StartStreamRequest) -> StreamInfo:
        source = self._resolve_source(req)
        protocol = self._resolve_protocol(req)
        stream_id = uuid.uuid4().hex[:12]
        with self._lock:
            if len(self._streams) >= self.settings.max_streams:
                raise StreamLimitReached(f"Maximum of {self.settings.max_streams} concurrent streams reached")
            if protocol is Protocol.HLS:
                stream: BaseStream = HlsStream(stream_id, req, source, self.settings)
            else:
                stream = MjpegStream(
                    stream_id, req, source, self.settings, capture_factory=self._capture_factory
                )
            self._streams[stream_id] = stream
        try:
            stream.start()
        except Exception:
            with self._lock:
                self._streams.pop(stream_id, None)
            raise
        log.info("Started stream %s (%s/%s)", stream_id, req.source_type.value, protocol.value)
        return stream.info()

    def stop(self, stream_id: str) -> StreamInfo:
        """Blocking: joins worker threads and removes temp files. Run via ``astop`` from async code."""
        with self._lock:
            stream = self._streams.pop(stream_id, None)
        if stream is None:
            raise StreamNotFound(f"Stream '{stream_id}' not found")
        stream.stop()
        log.info("Stopped stream %s", stream_id)
        return stream.info()

    async def astop(self, stream_id: str) -> StreamInfo:
        return await asyncio.to_thread(self.stop, stream_id)

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._streams)
        for stream_id in ids:
            try:
                self.stop(stream_id)
            except StreamNotFound:
                pass

    # -- helpers -----------------------------------------------------------
    def _resolve_source(self, req: StartStreamRequest):
        if req.source_type is SourceType.FILE:
            try:
                return resolve_media_file(self.settings.media_dir, req.source)
            except ValueError as exc:
                raise InvalidSource(str(exc)) from None
        if req.source_type is SourceType.WEBCAM:
            return int(req.source)
        return req.source  # RTSP URL, already validated by the request model

    def _resolve_protocol(self, req: StartStreamRequest) -> Protocol:
        if req.source_type is SourceType.WEBCAM:
            return Protocol.MJPEG
        if req.protocol is Protocol.HLS and not self.settings.ffmpeg_available:
            raise ProtocolUnavailable("HLS requires FFmpeg, which was not found. Install it or use mjpeg.")
        if req.protocol:
            return req.protocol
        return Protocol.HLS if self.settings.ffmpeg_available else Protocol.MJPEG
