"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.audio_routes import router as audio_router
from app.api.stream_routes import router
from app.core.auth import BasicAuthMiddleware
from app.core.config import Settings, get_settings
from app.core.errors import StreamError
from app.services.audio_hub import AudioHub
from app.services.stream_manager import StreamManager
from app.services.video_capture import CaptureFactory, default_capture_factory
from app.utils.video_utils import CredentialMaskingFilter, mask_url

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, CredentialMaskingFilter) for f in handler.filters):
            handler.addFilter(CredentialMaskingFilter())


def create_app(
    settings: Settings | None = None, capture_factory: CaptureFactory = default_capture_factory
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = StreamManager(settings, capture_factory)
        app.state.audio_hub = AudioHub(max_listeners=settings.max_listeners)
        yield
        app.state.manager.stop_all()

    app = FastAPI(title="Video Streaming API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    app.include_router(audio_router)

    @app.exception_handler(StreamError)
    async def stream_error_handler(_: Request, exc: StreamError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        # Default handler echoes the submitted input, which could contain RTSP credentials.
        errors = [
            {"loc": list(e["loc"]), "msg": mask_url(str(e["msg"])), "type": e["type"]} for e in exc.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    if settings.auth_password:
        app.add_middleware(BasicAuthMiddleware, username=settings.auth_user, password=settings.auth_password.get_secret_value())
    return app


app = create_app()
