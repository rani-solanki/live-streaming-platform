"""Request/response schemas."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.video_utils import validate_rtsp_url


class SourceType(str, Enum):
    FILE = "file"
    WEBCAM = "webcam"
    RTSP = "rtsp"


class Protocol(str, Enum):
    MJPEG = "mjpeg"
    HLS = "hls"


class StreamState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    ENDED = "ended"
    ERROR = "error"
    STOPPED = "stopped"


class StartStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source: str = Field(
        min_length=1,
        max_length=2048,
        description="file: path relative to the media dir; webcam: device index; rtsp: rtsp:// URL",
    )
    protocol: Protocol | None = Field(
        None, description="Defaults to HLS for file/RTSP when FFmpeg is installed, else MJPEG"
    )
    fps: int | None = Field(None, ge=1, le=60)
    width: int | None = Field(None, ge=64, le=3840)
    height: int | None = Field(None, ge=64, le=3840)
    loop: bool = Field(True, description="Restart video files when they end")

    @model_validator(mode="after")
    def _check_source(self) -> "StartStreamRequest":
        self.source = self.source.strip()
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must be provided together")
        if self.width is not None and (self.width % 2 or self.height % 2):
            raise ValueError("width and height must be even numbers")
        if self.source_type is SourceType.WEBCAM:
            if not (self.source.isdigit() and int(self.source) < 10):
                raise ValueError("webcam source must be a device index between 0 and 9")
            if self.protocol is Protocol.HLS:
                raise ValueError("webcam sources only support the mjpeg protocol")
        elif self.source_type is SourceType.RTSP:
            validate_rtsp_url(self.source)
        return self


class StreamInfo(BaseModel):
    stream_id: str
    source_type: SourceType
    source: str = Field(description="Display label; credentials are never included")
    protocol: Protocol
    state: StreamState
    error: str | None = None
    fps: int
    width: int | None = None
    height: int | None = None
    loop: bool
    reconnect_count: int = 0
    frames_published: int | None = Field(None, description="MJPEG only")
    viewers: int = Field(0, description="Connected MJPEG viewers")
    started_at: datetime
    uptime_seconds: float
    video_url: str
    playback_url: str


class StreamListResponse(BaseModel):
    count: int
    streams: list[StreamInfo]


class MediaListResponse(BaseModel):
    files: list[str]
    ffmpeg_available: bool


class ErrorResponse(BaseModel):
    detail: str
