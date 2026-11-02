import base64

import pytest
from pydantic import SecretStr
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import FILE_REQ


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def secured(settings):
    s = settings.model_copy(update={"auth_user": "admin", "auth_password": SecretStr("s3cret-pass")})
    with TestClient(create_app(s)) as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/api/streams", "/api/media", "/docs", "/hls/x/index.m3u8", "/api/streams/x/video"])
def test_requires_credentials(secured, path):
    r = secured.get(path)
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]


def test_start_requires_credentials(secured):
    assert secured.post("/api/streams/start", json=FILE_REQ).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [basic("admin", "wrong"), basic("root", "s3cret-pass"), {"Authorization": "Basic !!!notbase64"},
     {"Authorization": "Bearer s3cret-pass"}, basic("admin", "")],
)
def test_wrong_credentials_rejected(secured, headers):
    assert secured.get("/api/streams", headers=headers).status_code == 401


def test_correct_credentials_work_end_to_end(secured):
    h = basic("admin", "s3cret-pass")
    assert secured.get("/", headers=h).status_code == 200
    sid = secured.post("/api/streams/start", json=FILE_REQ, headers=h).json()["stream_id"]
    assert secured.get(f"/api/streams/{sid}/status", headers=h).status_code == 200
    assert secured.post(f"/api/streams/stop/{sid}", headers=h).status_code == 200


def test_health_is_public(secured):
    assert secured.get("/api/health").status_code == 200


def test_auth_disabled_by_default(client):
    assert client.get("/api/streams").status_code == 200
