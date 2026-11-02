"""Application settings, loaded from environment variables (prefix ``VS_``) or ``.env``."""

import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="VS_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    log_level: str = "INFO"

    # Stream limits / defaults
    max_streams: int = Field(8, ge=1, le=256)
    default_fps: int = Field(15, ge=1, le=60)
    jpeg_quality: int = Field(80, ge=1, le=100)

    # HTTP Basic auth: enabled when a password is set. Strongly recommended for anything beyond localhost.
    auth_user: str = "admin"
    auth_password: SecretStr | None = None

    # Live voice
    max_listeners: int = Field(50, ge=1, le=1000)
    # Extra origins allowed to open voice WebSockets (same-origin is always allowed), e.g. ["https://my.site"]
    allowed_origins: list[str] = []

    # Where playable video files live. Only files inside this directory can be streamed.
    media_dir: Path = Path("media")
    # Scratch space for HLS segments (use tmpfs/RAM disk in production). Dedicate it to one instance.
    hls_dir: Path = Path(tempfile.gettempdir()) / "vs_hls"

    # FFmpeg / HLS (FFmpeg >= 5.0 is required for RTSP timeouts to work)
    ffmpeg_path: str = "ffmpeg"
    hls_segment_seconds: int = Field(2, ge=1, le=10)
    hls_list_size: int = Field(5, ge=2, le=30)

    # Reconnection (RTSP / webcam). max_reconnect_attempts=0 means retry forever.
    reconnect_initial_delay: float = Field(1.0, gt=0)
    reconnect_max_delay: float = Field(30.0, gt=0)
    max_reconnect_attempts: int = Field(0, ge=0)
    rtsp_timeout_seconds: float = Field(5.0, gt=0)

    @property
    def ffmpeg_available(self) -> bool:
        return shutil.which(self.ffmpeg_path) is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
