"""Optional HTTP Basic authentication, enabled when ``VS_AUTH_PASSWORD`` is set.

Implemented as a pure ASGI middleware (not BaseHTTPMiddleware) so MJPEG/HLS streaming responses are
not buffered. Browsers cache Basic credentials per origin, so <img>/<video>/fetch calls just work.
"""

import base64
import binascii
import secrets

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PATHS = {"/api/health"}  # liveness probes (docker healthcheck) carry no credentials


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, username: str, password: str):
        self.app = app
        self._user = username.encode()
        self._password = password.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        if self._authorized(dict(scope["headers"]).get(b"authorization", b"")):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            response = PlainTextResponse(
                "Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Video Streaming"'}
            )
            await response(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})

    def _authorized(self, header: bytes) -> bool:
        scheme, _, token = header.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            user, _, password = base64.b64decode(token, validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        # evaluate both comparisons so timing doesn't reveal which one failed
        user_ok = secrets.compare_digest(user, self._user)
        pass_ok = secrets.compare_digest(password, self._password)
        return user_ok and pass_ok
