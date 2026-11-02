"""Common state/metadata shared by every stream implementation."""

import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.models.stream_models import (
    Protocol,
    SourceType,
    StartStreamRequest,
    StreamInfo,
    StreamState,
)
from app.utils.video_utils import display_source, mask_url


class BaseStream:
    protocol: Protocol

    def __init__(
        self, stream_id: str, request: StartStreamRequest, resolved_source: str | Path | int, settings: Settings
    ):
        self.id = stream_id
        self.request = request
        self.resolved_source = resolved_source
        self.settings = settings
        self.source_type = request.source_type
        self.fps = request.fps or settings.default_fps
        self.reconnect_count = 0
        self.created_at = datetime.now(timezone.utc)
        self._state_lock = threading.Lock()
        self._state = StreamState.STARTING
        self._error: str | None = None

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    def set_state(self, state: StreamState, error: str | None = None) -> None:
        with self._state_lock:
            if self._state is StreamState.STOPPED:  # a stopped stream never comes back
                return
            self._state = state
            self._error = mask_url(error) if error else None

    # -- hooks for subclasses ---------------------------------------------
    frames_published: int | None = None
    viewers: int = 0

    @property
    def playback_url(self) -> str:
        return f"/api/streams/{self.id}/video"

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    # -- reporting ---------------------------------------------------------
    def info(self) -> StreamInfo:
        req = self.request
        return StreamInfo(
            stream_id=self.id,
            source_type=self.source_type,
            source=display_source(self.source_type.value, req.source),
            protocol=self.protocol,
            state=self.state,
            error=self.error,
            fps=self.fps,
            width=req.width,
            height=req.height,
            loop=req.loop,
            reconnect_count=self.reconnect_count,
            frames_published=self.frames_published,
            viewers=self.viewers,
            started_at=self.created_at,
            uptime_seconds=round((datetime.now(timezone.utc) - self.created_at).total_seconds(), 1),
            video_url=f"/api/streams/{self.id}/video",
            playback_url=self.playback_url,
        )

    @property
    def is_finished(self) -> bool:
        return self.state in (StreamState.ENDED, StreamState.ERROR, StreamState.STOPPED)


def backoff_delays(initial: float, maximum: float):
    """Yield exponentially growing delays: initial, 2*initial, ... capped at maximum."""
    delay = initial
    while True:
        yield delay
        delay = min(delay * 2, maximum)
