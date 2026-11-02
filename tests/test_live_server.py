"""End-to-end over real HTTP connections: verifies the viewer count shown in the UI."""

import threading
import time

import httpx
import pytest
import uvicorn

from app.main import create_app
from tests.conftest import FILE_REQ, wait_for


@pytest.fixture
def base_url(settings):
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started, timeout=10)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


def viewers(base, sid):
    return httpx.get(f"{base}/api/streams/{sid}/status").json()["viewers"]


def test_viewer_count_tracks_real_connections(base_url):
    sid = httpx.post(f"{base_url}/api/streams/start", json=FILE_REQ).json()["stream_id"]
    wait_for(lambda: httpx.get(f"{base_url}/api/streams/{sid}/status").json()["state"] == "running")
    assert viewers(base_url, sid) == 0

    with httpx.Client(timeout=10) as c1, httpx.Client(timeout=10) as c2:
        with c1.stream("GET", f"{base_url}/api/streams/{sid}/video") as r1:
            assert r1.headers["content-type"].startswith("multipart/x-mixed-replace")
            it1 = r1.iter_raw()  # keep a reference: a garbage-collected iterator closes the connection
            chunk = next(it1)
            assert b"--frame" in chunk
            wait_for(lambda: viewers(base_url, sid) == 1)

            with c2.stream("GET", f"{base_url}/api/streams/{sid}/video") as r2:
                it2 = r2.iter_raw()
                next(it2)
                wait_for(lambda: viewers(base_url, sid) == 2)
            # second viewer disconnected -> back to 1
            wait_for(lambda: viewers(base_url, sid) == 1)
    wait_for(lambda: viewers(base_url, sid) == 0)  # all gone

    # listing agrees with status
    listed = httpx.get(f"{base_url}/api/streams").json()["streams"][0]
    assert listed["viewers"] == 0 and listed["frames_published"] > 0
    httpx.post(f"{base_url}/api/streams/stop/{sid}")


def test_stopping_stream_ends_open_viewer_connection(base_url):
    sid = httpx.post(f"{base_url}/api/streams/start", json=FILE_REQ).json()["stream_id"]
    wait_for(lambda: httpx.get(f"{base_url}/api/streams/{sid}/status").json()["state"] == "running")
    with httpx.stream("GET", f"{base_url}/api/streams/{sid}/video", timeout=10) as r:
        it = r.iter_raw()
        next(it)
        httpx.post(f"{base_url}/api/streams/stop/{sid}")
        deadline = time.monotonic() + 10
        for _ in it:  # connection must close by itself
            assert time.monotonic() < deadline
    assert httpx.get(f"{base_url}/api/streams/{sid}/status").status_code == 404


def test_many_viewers_on_one_stream_share_it(base_url):
    sid = httpx.post(f"{base_url}/api/streams/start", json=FILE_REQ).json()["stream_id"]
    wait_for(lambda: httpx.get(f"{base_url}/api/streams/{sid}/status").json()["state"] == "running")
    clients = [httpx.Client(timeout=10) for _ in range(10)]
    streams = [c.stream("GET", f"{base_url}/api/streams/{sid}/video") for c in clients]
    try:
        responses = [s.__enter__() for s in streams]
        iterators = [r.iter_raw() for r in responses]  # keep references alive
        for it in iterators:
            next(it)
        wait_for(lambda: viewers(base_url, sid) == 10)
    finally:
        for s in streams:
            s.__exit__(None, None, None)
        for c in clients:
            c.close()
    wait_for(lambda: viewers(base_url, sid) == 0)
