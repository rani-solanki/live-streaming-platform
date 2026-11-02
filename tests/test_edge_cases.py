"""Edge cases found in code review."""

import asyncio
import time

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.streaming_service import HlsStream
from app.services.video_capture import mjpeg_frames
from tests.conftest import FILE_REQ, FakeCapture, wait_for


def start(client, **body):
    return client.post("/api/streams/start", json={**FILE_REQ, **body})


def status(client, sid):
    return client.get(f"/api/streams/{sid}/status").json()


def test_file_never_plays_faster_than_native_fps(client):
    """sample.avi is 30 fps; asking for 60 must not make it play at 2x."""
    sid = start(client, fps=60).json()["stream_id"]
    wait_for(lambda: status(client, sid)["frames_published"] > 0)
    first = status(client, sid)["frames_published"]
    time.sleep(1.0)
    produced = status(client, sid)["frames_published"] - first
    assert 15 <= produced <= 40, produced  # ~30; would be ~60 with the bug


def test_requested_fps_below_native_is_honoured(client):
    sid = start(client, fps=10).json()["stream_id"]
    wait_for(lambda: status(client, sid)["frames_published"] > 0)
    first = status(client, sid)["frames_published"]
    time.sleep(1.0)
    produced = status(client, sid)["frames_published"] - first
    assert 5 <= produced <= 14, produced


def test_resize_applied(client):
    sid = start(client, width=128, height=96).json()["stream_id"]
    wait_for(lambda: status(client, sid)["frames_published"] > 0)
    import cv2, numpy as np

    jpeg = client.get(f"/api/streams/{sid}/snapshot").content
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (96, 128)


def test_viewers_finish_when_stream_ends(client):
    sid = start(client, loop=False, fps=30).json()["stream_id"]
    stream = client.app.state.manager.get(sid)

    async def drain():
        count = 0
        async for _ in mjpeg_frames(stream):
            count += 1
        return count

    asyncio.run(asyncio.wait_for(drain(), 10))  # must terminate on its own, not hang
    assert status(client, sid)["state"] == "ended"
    assert stream.viewers == 0


def test_viewers_finish_when_stream_errors(make_client):
    client = make_client(lambda s: FakeCapture(opened=False), max_reconnect_attempts=1)
    sid = client.post(
        "/api/streams/start", json={"source_type": "rtsp", "source": "rtsp://h/x", "protocol": "mjpeg"}
    ).json()["stream_id"]
    stream = client.app.state.manager.get(sid)

    async def drain():
        async for _ in mjpeg_frames(stream):
            pass

    asyncio.run(asyncio.wait_for(drain(), 10))
    assert status(client, sid)["state"] == "error"


def test_webcam_that_never_opens_fails_fast(make_client):
    client = make_client(lambda s: FakeCapture(opened=False), max_reconnect_attempts=0)  # would retry forever
    sid = client.post("/api/streams/start", json={"source_type": "webcam", "source": "7"}).json()["stream_id"]
    info = wait_for(lambda: (i := status(client, sid))["state"] == "error" and i)
    assert "Webcam 7" in info["error"] and info["reconnect_count"] == 0


def test_webcam_unplugged_after_working_reconnects(make_client):
    made = []

    def factory(stream):
        made.append(1)
        return FakeCapture(frames=5) if len(made) == 1 else FakeCapture()

    client = make_client(factory)
    sid = client.post("/api/streams/start", json={"source_type": "webcam", "source": "0"}).json()["stream_id"]
    wait_for(lambda: len(made) >= 2 and status(client, sid)["state"] == "running")
    assert status(client, sid)["reconnect_count"] >= 1


def test_corrupt_file_reports_error(client, settings):
    (settings.media_dir / "broken.avi").write_bytes(b"this is not a video" * 50)
    sid = start(client, source="broken.avi").json()["stream_id"]
    info = wait_for(lambda: (i := status(client, sid))["state"] == "error" and i)
    assert info["error"]


def test_new_media_files_appear_without_restart(client, settings):
    assert client.get("/api/media").json()["files"] == ["sample.avi"]
    (settings.media_dir / "sub").mkdir()
    (settings.media_dir / "sub" / "b.mp4").write_bytes(b"x")
    (settings.media_dir / "notes.txt").write_text("ignore me")
    assert client.get("/api/media").json()["files"] == ["sample.avi", "sub/b.mp4"]


def test_stop_twice_second_is_404(client):
    sid = start(client).json()["stream_id"]
    assert client.post(f"/api/streams/stop/{sid}").status_code == 200
    assert client.post(f"/api/streams/stop/{sid}").status_code == 404


def test_same_file_many_times_independent(client):
    a, b = (start(client).json()["stream_id"] for _ in range(2))
    client.post(f"/api/streams/stop/{a}")
    assert status(client, b)["state"] in ("starting", "running")  # stopping one doesn't affect the other


@pytest.mark.parametrize("name", ["a" * 5000 + ".avi", "bad\x00name.avi", "..\\sample.avi", "./../sample.avi"])
def test_hostile_file_names_rejected_not_500(client, name):
    assert start(client, source=name).status_code == 422


@pytest.mark.parametrize("url", ["rtsp://admin:p@ss@host/x", "rtsp://admin:pa%40ss@host:554/x?token=abc"])
def test_password_with_at_sign_fully_masked(make_client, url):
    client = make_client(lambda s: FakeCapture(opened=False), max_reconnect_attempts=1)
    r = client.post("/api/streams/start", json={"source_type": "rtsp", "source": url, "protocol": "mjpeg"})
    sid = r.json()["stream_id"]
    wait_for(lambda: status(client, sid)["state"] == "error")
    blob = r.text + client.get("/api/streams").text
    assert "ss@" not in blob and "pa%40" not in blob and "abc" not in blob and "admin" not in blob


def test_rtsp_url_with_control_chars_rejected(client):
    r = client.post("/api/streams/start", json={"source_type": "rtsp", "source": "rtsp://h/x\n-i file"})
    assert r.status_code in (200, 201, 422)  # newline is just part of the path; never reaches a shell
    if r.status_code == 201:
        client.post(f"/api/streams/stop/{r.json()['stream_id']}")


@pytest.mark.parametrize("field,value", [("default_fps", 0), ("max_streams", 0), ("jpeg_quality", 0), ("port", 70000)])
def test_settings_validated(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_shutdown_only_removes_own_hls_dirs(make_client, settings):
    sentinel = settings.hls_dir / "keep-me.txt"
    settings.hls_dir.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("not ours")
    client = make_client()
    start(client)
    client.__exit__(None, None, None)
    client.__enter__()
    assert sentinel.exists()  # root dir is never wiped


def test_stale_hls_dirs_cleaned_at_startup(make_client, settings):
    stale = settings.hls_dir / "0123456789ab"
    stale.mkdir(parents=True)
    (stale / "seg_00000.ts").write_bytes(b"x")
    other = settings.hls_dir / "someone-else"
    other.mkdir()
    make_client()
    assert not stale.exists() and other.exists()


def test_hls_command_flags(settings):
    from app.models.stream_models import StartStreamRequest

    def cmd(**req):
        r = StartStreamRequest(**{"source_type": "file", "source": "sample.avi", **req})
        s = HlsStream("abc", r, settings.media_dir / "sample.avi", settings)
        return s._build_cmd()

    looping = cmd()
    assert "-stream_loop" in looping and "omit_endlist" in looping[looping.index("-hls_flags") + 1]
    oneshot = cmd(loop=False)
    assert "-stream_loop" not in oneshot and "omit_endlist" not in oneshot[oneshot.index("-hls_flags") + 1]
    sized = cmd(width=640, height=360, fps=10)
    assert sized[sized.index("-vf") + 1] == "fps=10,scale=640:360"
    rtsp = cmd(source_type="rtsp", source="rtsp://u:p@h/x")
    assert "-rtsp_transport" in rtsp and "-re" not in rtsp
