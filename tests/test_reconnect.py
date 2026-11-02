"""RTSP failures, disconnections, reconnection and credential hygiene."""

import logging

from tests.conftest import FakeCapture, wait_for

RTSP = {"source_type": "rtsp", "source": "rtsp://admin:s3cret@10.255.255.1:554/live", "protocol": "mjpeg"}


def status(client, sid):
    return client.get(f"/api/streams/{sid}/status").json()


def test_rtsp_connection_failure_gives_up_with_masked_error(make_client):
    client = make_client(lambda s: FakeCapture(opened=False))
    r = client.post("/api/streams/start", json=RTSP)
    assert r.status_code == 201  # connecting happens in the background
    sid = r.json()["stream_id"]

    info = wait_for(lambda: (i := status(client, sid))["state"] == "error" and i)
    assert info["reconnect_count"] == 3  # max_reconnect_attempts in test settings
    assert "Unable to open" in info["error"] and "gave up" in info["error"]
    assert "s3cret" not in str(info)


def test_rtsp_reports_reconnecting_state(make_client):
    client = make_client(lambda s: FakeCapture(opened=False), max_reconnect_attempts=0)  # retry forever
    sid = client.post("/api/streams/start", json=RTSP).json()["stream_id"]
    wait_for(lambda: status(client, sid)["state"] == "reconnecting")
    assert client.post(f"/api/streams/stop/{sid}").status_code == 200  # stoppable mid-retry


def test_disconnect_then_recover(make_client):
    captures = []

    def factory(stream):
        # first connection drops after 5 frames; later ones stay up
        cap = FakeCapture(frames=5) if not captures else FakeCapture()
        captures.append(cap)
        return cap

    client = make_client(factory)
    sid = client.post("/api/streams/start", json=RTSP).json()["stream_id"]
    wait_for(lambda: len(captures) >= 2 and status(client, sid)["state"] == "running")
    info = status(client, sid)
    assert info["reconnect_count"] >= 1
    before = info["frames_published"]
    wait_for(lambda: status(client, sid)["frames_published"] > before + 3)  # frames flow again
    assert captures[0].released  # the broken capture was cleaned up


def test_resources_released_on_stop(make_client):
    captures = []

    def factory(stream):
        captures.append(FakeCapture())
        return captures[-1]

    client = make_client(factory)
    sid = client.post("/api/streams/start", json=RTSP).json()["stream_id"]
    stream = client.app.state.manager.get(sid)
    wait_for(lambda: status(client, sid)["state"] == "running")
    client.post(f"/api/streams/stop/{sid}")
    assert not stream._thread.is_alive()
    assert all(c.released for c in captures)
    assert stream.buffer.closed


def test_credentials_never_exposed(make_client, caplog):
    caplog.set_level(logging.DEBUG)
    client = make_client(lambda s: FakeCapture(opened=False))
    ok = client.post("/api/streams/start", json=RTSP)
    sid = ok.json()["stream_id"]
    wait_for(lambda: status(client, sid)["state"] == "error")

    bodies = [ok.text, client.get("/api/streams").text, client.get(f"/api/streams/{sid}/status").text]
    # even validation errors must not echo the submitted URL
    bad = client.post("/api/streams/start", json={**RTSP, "fps": 0})
    bodies.append(bad.text)
    assert bad.status_code == 422
    assert all("s3cret" not in b and "admin:" not in b for b in bodies)
    assert "s3cret" not in caplog.text
