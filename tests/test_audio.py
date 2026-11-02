"""Live voice relay: speaker -> many listeners."""

import asyncio
import base64
import statistics
import struct
import time

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.services.audio_hub import MAX_FRAME_BYTES, SAMPLE_RATE, AudioHub
from tests.conftest import OPEN_SESSIONS, close_ws, ws_ctx


def frame(n: int, samples: int = 320) -> bytes:
    """A recognisable 20 ms frame: every sample equals n."""
    return struct.pack(f"<{samples}h", *([n] * samples))


def first_value(data: bytes) -> int:
    return struct.unpack("<h", data[:2])[0]


@pytest.fixture
def voice(client):
    return client


def listen(client, audio=True):
    """Connect a viewer. audio=True also presses "Listen" and waits for the server's acknowledgement."""
    ws = client.websocket_connect("/ws/audio/listen")
    session = ws.__enter__()
    hello = session.receive_json()
    assert hello["type"] == "state" and hello["sample_rate"] == SAMPLE_RATE
    OPEN_SESSIONS.append(session)
    if audio:
        set_listening(session, True)
    return session


def set_listening(session, on):
    session.send_json({"type": "listening", "on": on})
    while True:
        msg = session.receive_json()
        if msg.get("type") == "listening":
            assert msg["on"] is on
            return


def talk(client):
    session = client.websocket_connect("/ws/audio/talk").__enter__()
    assert session.receive_json() == {"type": "ready", "sample_rate": SAMPLE_RATE}
    OPEN_SESSIONS.append(session)
    return session


def next_audio(session):
    """Next binary frame, skipping state messages."""
    while True:
        msg = session.receive()
        if msg.get("bytes") is not None:
            return msg["bytes"]


def next_state(session):
    while True:
        msg = session.receive()
        if msg.get("text") is not None:
            import json

            data = json.loads(msg["text"])
            if data.get("type") == "state":
                return data


# ---- core behaviour -----------------------------------------------------------------------------
def test_status_when_idle(client):
    assert client.get("/api/audio/status").json() == {
        "talking": False, "muted": False, "listeners": 0, "listening": 0, "sample_rate": SAMPLE_RATE,
    }


def test_audio_reaches_all_listeners_in_order(voice):
    listeners = [listen(voice) for _ in range(5)]
    speaker = talk(voice)
    for n in range(1, 51):
        speaker.send_bytes(frame(n))
    for ls in listeners:
        values = [first_value(next_audio(ls)) for _ in range(50)]
        assert values == list(range(1, 51))  # every frame, in order, none duplicated


def test_status_counts_listeners_and_speaker(voice):
    a, b = listen(voice), listen(voice)
    status = voice.get("/api/audio/status").json()
    assert status["listeners"] == 2 and status["listening"] == 2 and status["talking"] is False
    speaker = talk(voice)
    assert voice.get("/api/audio/status").json()["talking"] is True
    close_ws(b)
    deadline = time.monotonic() + 3
    while voice.get("/api/audio/status").json()["listeners"] != 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert voice.get("/api/audio/status").json()["listeners"] == 1
    close_ws(speaker)
    close_ws(a)


def test_listener_joining_mid_speech_gets_state_and_audio(voice):
    speaker = talk(voice)
    speaker.send_bytes(frame(1))
    late = listen(voice)  # hello already says someone is talking
    speaker.send_bytes(frame(2))
    assert first_value(next_audio(late)) == 2


def test_listener_is_told_when_speaker_starts_and_stops(voice):
    ls = listen(voice)
    speaker = talk(voice)
    assert next_state(ls)["talking"] is True
    close_ws(speaker)
    assert next_state(ls)["talking"] is False


# ---- listening flag (who actually has audio on) ----------------------------------------------
def test_only_viewers_who_pressed_listen_get_audio(voice):
    on = listen(voice)
    off = listen(voice, audio=False)  # connected for status updates only
    speaker = talk(voice)
    for n in range(1, 6):
        speaker.send_bytes(frame(n))
    assert [first_value(next_audio(on)) for _ in range(5)] == [1, 2, 3, 4, 5]
    speaker.send_json({"type": "mute", "muted": True})
    # the viewer with audio off still learns about the speaker's state, but never got a frame
    seen = []
    while True:
        msg = off.receive()
        assert msg.get("bytes") is None, "audio sent to a viewer who did not press Listen"
        import json

        data = json.loads(msg["text"])
        seen.append(data)
        if data.get("muted"):
            break
    assert any(d["talking"] for d in seen)
    status = voice.get("/api/audio/status").json()
    assert status["listeners"] == 2 and status["listening"] == 1


def test_turning_listening_off_stops_audio_and_updates_count(voice):
    a, b = listen(voice), listen(voice)
    speaker = talk(voice)
    set_listening(b, False)
    assert voice.get("/api/audio/status").json()["listening"] == 1
    speaker.send_bytes(frame(1))
    assert first_value(next_audio(a)) == 1
    set_listening(b, True)
    assert voice.get("/api/audio/status").json()["listening"] == 2
    speaker.send_bytes(frame(2))
    assert first_value(next_audio(b)) == 2  # b only ever sees frames sent after it turned listening on


def test_malformed_listening_messages_are_ignored(voice):
    ls = listen(voice)
    ls.send_text("garbage")
    ls.send_json({"type": "nonsense"})
    ls.send_text("ping")
    speaker = talk(voice)
    speaker.send_bytes(frame(9))
    assert first_value(next_audio(ls)) == 9  # connection still healthy


# ---- mute ----------------------------------------------------------------------------------------
def test_mute_stops_audio_and_notifies_listeners(voice):
    ls = listen(voice)
    speaker = talk(voice)
    next_state(ls)  # talking
    speaker.send_bytes(frame(1))
    assert first_value(next_audio(ls)) == 1

    speaker.send_json({"type": "mute", "muted": True})
    assert next_state(ls)["muted"] is True
    speaker.send_bytes(frame(2))  # sent while muted: must NOT be relayed
    speaker.send_bytes(frame(3))

    speaker.send_json({"type": "mute", "muted": False})
    assert next_state(ls)["muted"] is False
    speaker.send_bytes(frame(4))
    assert first_value(next_audio(ls)) == 4  # 2 and 3 never arrived


def test_mute_resets_when_speaker_leaves(voice):
    speaker = talk(voice)
    speaker.send_json({"type": "mute", "muted": True})
    close_ws(speaker)
    deadline = time.monotonic() + 3
    while voice.get("/api/audio/status").json()["talking"] and time.monotonic() < deadline:
        time.sleep(0.02)
    s = voice.get("/api/audio/status").json()
    assert s["talking"] is False and s["muted"] is False


# ---- exclusivity / abuse ----------------------------------------------------------------------
def test_second_speaker_is_rejected_and_first_unaffected(voice):
    ls = listen(voice)
    first = talk(voice)
    with ws_ctx(voice, "/ws/audio/talk") as second:
        msg = second.receive_json()
        assert msg["type"] == "error" and msg["code"] == "busy"
    first.send_bytes(frame(7))
    assert first_value(next_audio(ls)) == 7


def test_speaker_slot_freed_after_disconnect(voice):
    close_ws(talk(voice))
    deadline = time.monotonic() + 3
    while voice.get("/api/audio/status").json()["talking"] and time.monotonic() < deadline:
        time.sleep(0.02)
    talk(voice)  # a new speaker can take over


@pytest.mark.parametrize("payload", [b"", b"\x00" * 3, b"\x00" * (MAX_FRAME_BYTES + 2)])
def test_bad_frames_close_the_speaker_connection(voice, payload):
    speaker = talk(voice)
    speaker.send_bytes(payload)
    with pytest.raises(WebSocketDisconnect):
        speaker.receive_json()


def test_malformed_control_message_closes_connection(voice):
    speaker = talk(voice)
    speaker.send_text("not json")
    with pytest.raises(WebSocketDisconnect):
        speaker.receive_json()


def test_flooding_speaker_is_cut_off(voice):
    speaker = talk(voice)
    with pytest.raises(WebSocketDisconnect):
        for _ in range(2000):  # ~25x real time
            speaker.send_bytes(frame(1, 320))
        speaker.receive_json()


def test_listener_limit(make_client):
    client = make_client(max_listeners=2)
    a, b = listen(client), listen(client)
    with ws_ctx(client, "/ws/audio/listen") as third:
        assert third.receive_json()["code"] == "full"
    close_ws(a)
    deadline = time.monotonic() + 3
    while client.get("/api/audio/status").json()["listeners"] >= 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    listen(client)  # space freed
    close_ws(b)


def test_latency_through_server_is_small(voice):
    ls = listen(voice)
    speaker = talk(voice)
    next_state(ls)
    samples = []
    for n in range(100):
        t0 = time.perf_counter()
        speaker.send_bytes(frame(n % 1000))
        next_audio(ls)
        samples.append((time.perf_counter() - t0) * 1000)
    assert statistics.median(samples) < 50, statistics.median(samples)  # relay hop, in ms


# ---- hub internals ----------------------------------------------------------------------------
def test_slow_listener_drops_oldest_and_never_blocks_others():
    async def run():
        hub = AudioHub(queue_size=10)
        fast, slow = hub.add_listener(), hub.add_listener()
        hub.set_listening(fast, True)
        hub.set_listening(slow, True)
        token = object()
        assert hub.claim_talker(token)
        got_fast = []
        for n in range(200):
            assert hub.publish(token, frame(n, 4))
            while not fast.queue.empty():  # fast listener drains continuously
                item = fast.queue.get_nowait()
                if isinstance(item, bytes):
                    got_fast.append(first_value(item))
        assert got_fast == list(range(200))  # fast listener lost nothing
        slow_frames = []
        while not slow.queue.empty():
            item = slow.queue.get_nowait()
            if isinstance(item, bytes):
                slow_frames.append(first_value(item))
        assert slow_frames[-1] == 199 and len(slow_frames) <= 10  # newest kept
        assert slow_frames == sorted(slow_frames) and slow.dropped > 0

    asyncio.run(run())


def test_only_the_active_speaker_can_publish():
    async def run():
        hub = AudioHub()
        a, b = object(), object()
        hub.claim_talker(a)
        assert not hub.claim_talker(b)
        assert not hub.publish(b, frame(1, 4))
        hub.release_talker(b)  # releasing someone else's slot is a no-op
        assert hub.talking

    asyncio.run(run())


# ---- security ------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/ws/audio/talk", "/ws/audio/listen"])
def test_cross_site_websocket_rejected(client, path):
    with pytest.raises(WebSocketDisconnect):
        with ws_ctx(client, path, headers={"origin": "https://evil.example"}):
            pass


@pytest.mark.parametrize("path", ["/ws/audio/talk", "/ws/audio/listen"])
def test_same_origin_websocket_allowed(client, path):
    with ws_ctx(client, path, headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] in ("ready", "state")


def test_forwarded_host_origin_allowed(client):
    """Behind a tunnel/proxy the public host arrives in X-Forwarded-Host."""
    headers = {"origin": "https://my-tunnel.example", "x-forwarded-host": "my-tunnel.example"}
    with ws_ctx(client, "/ws/audio/listen", headers=headers) as ws:
        assert ws.receive_json()["type"] == "state"


def test_allowed_origins_setting(make_client):
    client = make_client(allowed_origins=["https://partner.example"])
    with ws_ctx(client, "/ws/audio/listen", headers={"origin": "https://partner.example"}) as ws:
        assert ws.receive_json()["type"] == "state"


@pytest.fixture
def secured(settings):
    s = settings.model_copy(update={"auth_password": SecretStr("pw-123")})
    with TestClient(create_app(s)) as c:
        yield c


@pytest.mark.parametrize("path", ["/ws/audio/talk", "/ws/audio/listen"])
def test_websockets_require_login(secured, path):
    with pytest.raises(WebSocketDisconnect):
        with ws_ctx(secured, path):
            pass
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
    with pytest.raises(WebSocketDisconnect):
        with ws_ctx(secured, path, headers=bad):
            pass


def test_websocket_with_login_works_and_status_is_protected(secured):
    good = {"Authorization": "Basic " + base64.b64encode(b"admin:pw-123").decode()}
    assert secured.get("/api/audio/status").status_code == 401
    assert secured.get("/api/audio/status", headers=good).status_code == 200
    with ws_ctx(secured, "/ws/audio/listen", headers=good) as ws:
        assert ws.receive_json()["type"] == "state"
