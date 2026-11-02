import pytest

from app.utils.video_utils import display_source, mask_url, resolve_media_file, validate_rtsp_url


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("rtsp://admin:pw@10.0.0.5:554/s", "rtsp://***@10.0.0.5:554/s"),
        ("failed to open rtsp://u:p@h/x?token=abc&a=1", "failed to open rtsp://***@h/x?token=***&a=1"),
        ("no secrets here", "no secrets here"),
    ],
)
def test_mask_url(raw, expected):
    assert mask_url(raw) == expected


def test_display_source_hides_credentials():
    assert display_source("rtsp", "rtsp://a:b@cam.local:8554/ch1?token=x") == "rtsp://cam.local:8554/ch1"
    assert display_source("file", "a/b.mp4") == "a/b.mp4"


def test_validate_rtsp_url():
    assert validate_rtsp_url("rtsp://h/x")
    for bad in ("http://h/x", "rtsp://", "file:///etc/passwd", "rtsp://h:notaport/x"):
        with pytest.raises(ValueError):
            validate_rtsp_url(bad)


def test_resolve_media_file(tmp_path):
    (tmp_path / "ok.mp4").write_bytes(b"x")
    (tmp_path / "x.txt").write_bytes(b"x")
    assert resolve_media_file(tmp_path, "ok.mp4").name == "ok.mp4"
    for bad in ("../ok.mp4", "x.txt", "missing.mp4", "/etc/passwd"):
        with pytest.raises(ValueError):
            resolve_media_file(tmp_path, bad)
