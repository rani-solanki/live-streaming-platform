"""Helpers for source validation and credential masking."""

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}

# Greedy up to the *last* "@" of the authority, so passwords containing "@" are fully masked.
_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^/\s]*@", re.IGNORECASE)
_SECRET_PARAM = re.compile(r"(?P<k>(?:token|password|passwd|pwd|key|auth)=)[^&\s]+", re.IGNORECASE)


def mask_url(text: str) -> str:
    """Remove credentials (user:pass@ and secret query params) from any text."""
    text = _USERINFO.sub(r"\g<scheme>***@", text)
    return _SECRET_PARAM.sub(r"\g<k>***", text)


class CredentialMaskingFilter(logging.Filter):
    """Logging filter that scrubs credentials from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_url(record.getMessage())
        record.args = None
        return True


def validate_rtsp_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("rtsp", "rtsps"):
        raise ValueError("RTSP source must start with rtsp:// or rtsps://")
    try:
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise ValueError("RTSP URL has an invalid port") from None
    if not host:
        raise ValueError("RTSP URL has no host")
    return url


def display_source(source_type: str, source: str) -> str:
    """A safe-to-show label for a source: never includes credentials or query strings."""
    if source_type == "rtsp":
        parts = urlsplit(source)
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"
    return source


def resolve_media_file(media_dir: Path, name: str) -> Path:
    """Resolve ``name`` inside ``media_dir``; reject traversal, missing files, odd extensions."""
    root = media_dir.resolve()
    try:
        candidate = (root / name).resolve()
    except (OSError, ValueError):  # over-long names, NUL bytes, invalid characters
        raise ValueError("Invalid video file name") from None
    if root != candidate and root not in candidate.parents:
        raise ValueError("Video file must be inside the media directory")
    if candidate.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported video type; allowed: {sorted(ALLOWED_VIDEO_EXTENSIONS)}")
    try:
        found = candidate.is_file()
    except OSError:
        found = False
    if not found:
        raise ValueError("Video file not found in the media directory")
    return candidate


def list_media_files(media_dir: Path) -> list[str]:
    if not media_dir.is_dir():
        return []
    root = media_dir.resolve()
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in ALLOWED_VIDEO_EXTENSIONS
    )
