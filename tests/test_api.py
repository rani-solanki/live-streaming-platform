"""API-level tests: lifecycle, validation, concurrency, cleanup."""

import asyncio

import pytest

from app.services.video_capture import mjpeg_frames
from tests.conftest import FILE_REQ, wait_for


def start(client, **body):
    return client.post("/api/streams/start", json={**FILE_REQ, **body})


def test_start_status_snapshot_stop(client):
    r = start(client)
    assert r.status_code == 201
    info = r.json()
    sid = info["stream_id"]
    assert info["protocol"] == "mjpeg" and info["source"] == "sample.avi"
    assert info["video_url"] == f"/api/streams/{sid}/video"

    wait_for(lambda: client.get(f"/api/streams/{sid}/status").json()["frames_published"] > 0)
    assert client.get(f"/api/streams/{sid}/status").json()["state"] == "running"

    snap = client.get(f"/api/streams/{sid}/snapshot")
    assert snap.status_code == 200 and snap.content[:2] == b"\xff\xd8"

    listing = client.get("/api/streams").json()
    assert listing["count"] == 1 and listing["streams"][0]["stream_id"] == sid

    stopped = client.post(f"/api/streams/stop/{sid}")
    assert stopped.status_code == 200 and stopped.json()["state"] == "stopped"
    assert client.get(f"/api/streams/{sid}/status").status_code == 404
    assert client.get("/api/streams").json()["count"] == 0


def test_mjpeg_generator_yields_jpeg_parts(client):
    sid = start(client).json()["stream_id"]
    stream = client.app.state.manager.get(sid)

    async def first_chunk():
        gen = mjpeg_frames(stream)
        chunk = await asyncio.wait_for(anext(gen), 5)
        assert stream.viewers == 1
        await gen.aclose()
        return chunk

    chunk = asyncio.run(first_chunk())
    assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg")
    assert b"\xff\xd8" in chunk
    assert stream.viewers == 0  # viewer deregistered on disconnect


def test_non_looping_file_ends(client):
    sid = start(client, loop=False, fps=60).json()["stream_id"]
    wait_for(lambda: client.get(f"/api/streams/{sid}/status").json()["state"] == "ended")


@pytest.mark.parametrize(
    "body",
    [
        {"source": "missing.avi"},
        {"source": "../secret.avi"},
        {"source": "C:/Windows/notepad.exe"},
        {"source": "sample.txt"},
    ],
)
def test_invalid_file_sources_rejected(client, body):
    r = start(client, **body)
    assert r.status_code == 422
    assert client.get("/api/streams").json()["count"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"source_type": "rtsp", "source": "http://host/stream"},
        {"source_type": "rtsp", "source": "rtsp://"},
        {"source_type": "webcam", "source": "abc"},
        {"source_type": "webcam", "source": "0", "protocol": "hls"},
        {"fps": 0},
        {"fps": 500},
        {"width": 640},
        {"width": 641, "height": 480},
        {"protocol": "dash"},
        {"unknown_field": 1},
    ],
)
def test_request_validation(client, body):
    r = start(client, **body)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_missing_body_fields(client):
    assert client.post("/api/streams/start", json={}).status_code == 422


def test_unknown_stream_404(client):
    assert client.get("/api/streams/nope/status").status_code == 404
    assert client.get("/api/streams/nope/video").status_code == 404
    assert client.post("/api/streams/stop/nope").json() == {"detail": "Stream 'nope' not found"}


def test_hls_without_ffmpeg_is_503(client):
    assert start(client, protocol="hls").status_code == 503


def test_multiple_concurrent_streams_and_limit(client):
    ids = [start(client).json()["stream_id"] for _ in range(4)]
    assert len(set(ids)) == 4
    assert client.get("/api/streams").json()["count"] == 4
    assert start(client).status_code == 429

    for sid in ids:
        wait_for(lambda s=sid: client.get(f"/api/streams/{s}/status").json()["frames_published"] > 0)
    for sid in ids:
        assert client.post(f"/api/streams/stop/{sid}").status_code == 200
    assert client.get("/api/streams").json()["count"] == 0
    assert start(client).status_code == 201  # capacity freed


def test_shutdown_stops_all_threads(make_client):
    client = make_client()
    sids = [start(client).json()["stream_id"] for _ in range(3)]
    streams = [client.app.state.manager.get(s) for s in sids]
    client.__exit__(None, None, None)  # triggers lifespan shutdown
    client.__enter__()  # so the fixture teardown stays balanced
    assert all(not s._thread.is_alive() for s in streams)


def test_media_listing_and_health(client):
    media = client.get("/api/media").json()
    assert media["files"] == ["sample.avi"] and media["ffmpeg_available"] is False
    assert client.get("/api/health").json()["status"] == "ok"


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Video Streaming" in r.text
