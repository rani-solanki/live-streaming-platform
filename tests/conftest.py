import concurrent.futures
import contextlib
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def make_video(path: Path, frames: int = 30, fps: int = 30, size=(64, 48)) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert writer.isOpened()
    for i in range(frames):
        writer.write(np.full((size[1], size[0], 3), (i * 8) % 255, np.uint8))
    writer.release()


class FakeCapture:
    """Stand-in for cv2.VideoCapture: yields ``frames`` frames, then reports a disconnect."""

    def __init__(self, opened=True, frames=10**9, delay=0.005):
        self._opened, self._left, self._delay = opened, frames, delay
        self.released = False

    def isOpened(self):
        return self._opened

    def grab(self):
        if self._left <= 0:
            return False
        self._left -= 1
        time.sleep(self._delay)
        return True

    def retrieve(self):
        return True, np.zeros((48, 64, 3), np.uint8)

    def get(self, _prop):
        return 0

    def release(self):
        self.released = True


OPEN_SESSIONS: list = []  # websocket sessions to close before a test client shuts down


def close_ws(session) -> None:
    """Close a manually-entered websocket session properly (plain .close() leaves the server task running)."""
    try:
        session.__exit__(None, None, None)
    except concurrent.futures.CancelledError:
        pass  # TestClient cancels the server handler on exit; real servers don't
    if session in OPEN_SESSIONS:
        OPEN_SESSIONS.remove(session)


@contextlib.contextmanager
def ws_ctx(client, path, **kwargs):
    """`with`-style websocket session whose exit tolerates TestClient's handler cancellation."""
    session = client.websocket_connect(path, **kwargs).__enter__()
    OPEN_SESSIONS.append(session)
    try:
        yield session
    finally:
        close_ws(session)


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


@pytest.fixture
def settings(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    make_video(media / "sample.avi")
    return Settings(
        media_dir=media,
        hls_dir=tmp_path / "hls",
        max_streams=4,
        reconnect_initial_delay=0.01,
        reconnect_max_delay=0.05,
        max_reconnect_attempts=3,
        ffmpeg_path="definitely-not-ffmpeg",
    )


@pytest.fixture
def make_client(settings):
    clients = []

    def factory(capture_factory=None, **overrides):
        s = settings.model_copy(update=overrides)
        kwargs = {"capture_factory": capture_factory} if capture_factory else {}
        client = TestClient(create_app(s, **kwargs))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for ws in list(OPEN_SESSIONS):
        try:
            close_ws(ws)
        except Exception:  # noqa: BLE001 - already closed by the test
            pass
    OPEN_SESSIONS.clear()
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


FILE_REQ = {"source_type": "file", "source": "sample.avi", "protocol": "mjpeg", "fps": 30}
