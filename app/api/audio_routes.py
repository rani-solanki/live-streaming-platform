"""WebSocket endpoints for live voice: /ws/audio/talk (speaker) and /ws/audio/listen (viewers)."""

import asyncio
import json
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, WebSocket
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.services.audio_hub import MAX_BYTES_PER_SECOND, MAX_FRAME_BYTES, SAMPLE_RATE, AudioHub

log = logging.getLogger(__name__)
router = APIRouter()

WS_BAD_DATA, WS_POLICY, WS_TOO_BIG, WS_BUSY = 1003, 1008, 1009, 1013


def get_hub(conn: HTTPConnection) -> AudioHub:
    return conn.app.state.audio_hub


def origin_allowed(ws: WebSocket) -> bool:
    """Block cross-site WebSocket hijacking: a browser sends cached Basic-auth credentials with a
    WebSocket opened by *any* page, so a foreign page must not be able to use the microphone or listen in.
    Browsers always send Origin; non-browser clients (no Origin) are not subject to this attack."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    origin_host = urlsplit(origin).netloc.lower()
    allowed_hosts = {ws.headers.get("host", "").lower(), ws.headers.get("x-forwarded-host", "").lower()}
    extra = {urlsplit(o).netloc.lower() or o.lower() for o in ws.app.state.settings.allowed_origins}
    return origin_host in (allowed_hosts | extra) - {""}


async def _refuse(ws: WebSocket, code: str, message: str) -> None:
    try:
        await ws.send_json({"type": "error", "code": code, "message": message})
        await ws.close(WS_BUSY)
    except (RuntimeError, WebSocketDisconnect):  # client already gone
        pass


@router.get("/api/audio/status")
async def audio_status(hub: AudioHub = Depends(get_hub)):
    s = hub.state()
    s.pop("type")
    return s


@router.websocket("/ws/audio/talk")
async def talk(ws: WebSocket, hub: AudioHub = Depends(get_hub)):
    if not origin_allowed(ws):
        await ws.close(WS_POLICY)
        return
    await ws.accept()
    token = object()
    if not hub.claim_talker(token):
        await _refuse(ws, "busy", "Someone else is already speaking.")
        return
    log.info("Speaker connected")
    window_start, window_bytes = time.monotonic(), 0
    try:
        await ws.send_json({"type": "ready", "sample_rate": SAMPLE_RATE})
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data, text = msg.get("bytes"), msg.get("text")
            if data is not None:
                if not data or len(data) > MAX_FRAME_BYTES or len(data) % 2:
                    await ws.close(WS_TOO_BIG if len(data) > MAX_FRAME_BYTES else WS_BAD_DATA)
                    break
                now = time.monotonic()
                if now - window_start >= 1.0:
                    window_start, window_bytes = now, 0
                window_bytes += len(data)
                if window_bytes > MAX_BYTES_PER_SECOND:
                    await ws.close(WS_POLICY)
                    break
                hub.publish(token, data)
            elif text is not None:
                try:
                    cmd = json.loads(text)
                    if cmd.get("type") == "mute":
                        hub.set_muted(token, bool(cmd.get("muted")))
                except (ValueError, AttributeError):
                    await ws.close(WS_BAD_DATA)
                    break
    except WebSocketDisconnect:
        pass
    finally:
        hub.release_talker(token)
        log.info("Speaker disconnected")


@router.websocket("/ws/audio/listen")
async def listen(ws: WebSocket, hub: AudioHub = Depends(get_hub)):
    if not origin_allowed(ws):
        await ws.close(WS_POLICY)
        return
    await ws.accept()
    listener = hub.add_listener()
    if listener is None:
        await _refuse(ws, "full", "Too many listeners right now.")
        return

    async def sender():
        while True:
            item = await listener.queue.get()
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                await ws.send_json(item)

    async def receiver():  # detects disconnects; handles {"type":"listening","on":bool} and "ping" keepalives
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            text = msg.get("text")
            if text and text != "ping":
                try:
                    cmd = json.loads(text)
                except ValueError:
                    continue
                if isinstance(cmd, dict) and cmd.get("type") == "listening":
                    on = bool(cmd.get("on"))
                    hub.set_listening(listener, on)
                    listener.queue.put_nowait({"type": "listening", "on": on})  # ack: audio will flow from now on

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        await ws.send_json(hub.state())
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove_listener(listener)  # synchronous first: must happen even if this task is cancelled
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if ws.application_state is WebSocketState.CONNECTED:
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):  # client already gone
                pass
