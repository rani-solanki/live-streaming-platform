"""REST endpoints for stream management and playback."""

import asyncio
import re

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from app.core.errors import StreamNotFound
from app.models.stream_models import (
    ErrorResponse,
    MediaListResponse,
    StartStreamRequest,
    StreamInfo,
    StreamListResponse,
)
from app.services.stream_manager import StreamManager
from app.services.streaming_service import HlsStream
from app.services.video_capture import MjpegStream, mjpeg_frames
from app.utils.video_utils import list_media_files

router = APIRouter()
_HLS_FILE = re.compile(r"^[A-Za-z0-9_.\-]+\.(m3u8|ts)$")
_NOT_FOUND = {404: {"model": ErrorResponse}}


def get_manager(request: Request) -> StreamManager:
    return request.app.state.manager


@router.post(
    "/api/streams/start",
    response_model=StreamInfo,
    status_code=201,
    responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def start_stream(body: StartStreamRequest, manager: StreamManager = Depends(get_manager)):
    return manager.start(body)


@router.post("/api/streams/stop/{stream_id}", response_model=StreamInfo, responses=_NOT_FOUND)
async def stop_stream(stream_id: str, manager: StreamManager = Depends(get_manager)):
    return await manager.astop(stream_id)


@router.get("/api/streams", response_model=StreamListResponse)
async def list_streams(manager: StreamManager = Depends(get_manager)):
    streams = manager.list()
    return StreamListResponse(count=len(streams), streams=streams)


@router.get("/api/streams/{stream_id}/status", response_model=StreamInfo, responses=_NOT_FOUND)
async def stream_status(stream_id: str, manager: StreamManager = Depends(get_manager)):
    return manager.get(stream_id).info()


@router.get(
    "/api/streams/{stream_id}/video",
    responses={
        200: {"content": {"multipart/x-mixed-replace": {}}, "description": "MJPEG stream"},
        307: {"description": "HLS streams redirect to their playlist"},
        **_NOT_FOUND,
    },
)
async def stream_video(stream_id: str, manager: StreamManager = Depends(get_manager)):
    stream = manager.get(stream_id)
    if isinstance(stream, HlsStream):
        return RedirectResponse(stream.playback_url, status_code=307)
    assert isinstance(stream, MjpegStream)
    return StreamingResponse(
        mjpeg_frames(stream),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/streams/{stream_id}/snapshot", responses={200: {"content": {"image/jpeg": {}}}, **_NOT_FOUND})
async def stream_snapshot(stream_id: str, manager: StreamManager = Depends(get_manager)):
    stream = manager.get(stream_id)
    if not isinstance(stream, MjpegStream):
        raise StreamNotFound("Snapshots are only available for MJPEG streams")
    item = await asyncio.to_thread(stream.buffer.jpeg)
    if item is None:
        return Response(status_code=204)
    return Response(item[1], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/hls/{stream_id}/{filename}", include_in_schema=False)
async def hls_file(stream_id: str, filename: str, manager: StreamManager = Depends(get_manager)):
    stream = manager.get(stream_id)
    if not isinstance(stream, HlsStream) or not _HLS_FILE.match(filename):
        raise StreamNotFound("Not found")
    path = stream.out_dir / filename
    if not path.is_file():
        raise StreamNotFound("Not found")
    if filename.endswith(".m3u8"):
        return FileResponse(
            path, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"}
        )
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "max-age=30"})


@router.get("/api/media", response_model=MediaListResponse)
async def list_media(manager: StreamManager = Depends(get_manager)):
    return MediaListResponse(
        files=await asyncio.to_thread(list_media_files, manager.settings.media_dir),
        ffmpeg_available=manager.settings.ffmpeg_available
    )


@router.get("/api/health")
async def health(manager: StreamManager = Depends(get_manager)):
    return {"status": "ok", "active_streams": len(manager.list())}
