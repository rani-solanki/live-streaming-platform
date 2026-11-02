"""HLS supervisor tests. A stub replaces FFmpeg so they run anywhere; one real-FFmpeg test is skipped if absent."""

import shutil
import sys

import pytest

from app.services.streaming_service import HlsStream
from tests.conftest import wait_for

STUB = (
    "import pathlib,sys,time;"
    "d=pathlib.Path(sys.argv[1]);"
    "(d/'index.m3u8').write_text('#EXTM3U\\n#EXT-X-VERSION:3\\n');"
    "(d/'seg_00000.ts').write_bytes(b'x');"
    "time.sleep(60)"
)

FILE = {"source_type": "file", "source": "sample.avi", "protocol": "hls"}
RTSP = {"source_type": "rtsp", "source": "rtsp://u:pw123@10.255.255.1/s", "protocol": "hls"}


@pytest.fixture
def stub_ffmpeg(monkeypatch):
    monkeypatch.setattr("app.core.config.shutil.which", lambda _: "/stub")
    monkeypatch.setattr(HlsStream, "_build_cmd", lambda self: [sys.executable, "-c", STUB, str(self.out_dir)])


def test_hls_lifecycle_and_cleanup(stub_ffmpeg, client):
    sid = client.post("/api/streams/start", json=FILE).json()["stream_id"]
    stream = client.app.state.manager.get(sid)
    wait_for(lambda: client.get(f"/api/streams/{sid}/status").json()["state"] == "running")

    info = client.get(f"/api/streams/{sid}/status").json()
    assert info["protocol"] == "hls" and info["playback_url"] == f"/hls/{sid}/index.m3u8"

    pl = client.get(info["playback_url"])
    assert pl.status_code == 200 and pl.text.startswith("#EXTM3U")
    assert client.get(f"/hls/{sid}/seg_00000.ts").status_code == 200
    assert client.get(f"/hls/{sid}/..%2Fsecret").status_code == 404
    assert client.get(f"/hls/{sid}/notes.txt").status_code == 404

    redirect = client.get(f"/api/streams/{sid}/video", follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"] == info["playback_url"]

    proc, out_dir = stream._proc, stream.out_dir
    assert client.post(f"/api/streams/stop/{sid}").status_code == 200
    assert proc.poll() is not None  # ffmpeg process terminated
    assert not out_dir.exists()  # segments deleted
    assert client.get(f"/hls/{sid}/index.m3u8").status_code == 404


def test_hls_rtsp_ffmpeg_crash_restarts_then_errors(monkeypatch, client):
    monkeypatch.setattr("app.core.config.shutil.which", lambda _: "/stub")
    monkeypatch.setattr(
        HlsStream, "_build_cmd", lambda self: [sys.executable, "-c", "import sys; sys.exit('rtsp://u:pw123@h failed')"]
    )
    sid = client.post("/api/streams/start", json=RTSP).json()["stream_id"]
    info = wait_for(lambda: (i := client.get(f"/api/streams/{sid}/status").json())["state"] == "error" and i)
    assert info["reconnect_count"] == 3
    assert "pw123" not in str(info)


def test_hls_file_error_reported(monkeypatch, client):
    monkeypatch.setattr("app.core.config.shutil.which", lambda _: "/stub")
    monkeypatch.setattr(HlsStream, "_build_cmd", lambda self: [sys.executable, "-c", "import sys; sys.exit('bad input')"])
    sid = client.post("/api/streams/start", json=FILE).json()["stream_id"]
    info = wait_for(lambda: (i := client.get(f"/api/streams/{sid}/status").json())["state"] == "error" and i)
    assert "bad input" in info["error"]


def test_hls_missing_ffmpeg_binary_reports_error(monkeypatch, client):
    monkeypatch.setattr("app.core.config.shutil.which", lambda _: "/stub")  # pretend present; launch will fail
    sid = client.post("/api/streams/start", json=FILE).json()["stream_id"]
    info = wait_for(lambda: (i := client.get(f"/api/streams/{sid}/status").json())["state"] == "error" and i)
    assert "ffmpeg" in info["error"].lower()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_produces_playlist(make_client):
    client = make_client(ffmpeg_path="ffmpeg")
    sid = client.post("/api/streams/start", json={**FILE, "fps": 10}).json()["stream_id"]
    wait_for(lambda: client.get(f"/api/streams/{sid}/status").json()["state"] == "running", timeout=20)
    assert client.get(f"/hls/{sid}/index.m3u8").text.startswith("#EXTM3U")
    client.post(f"/api/streams/stop/{sid}")
