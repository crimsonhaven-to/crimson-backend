"""A cover's type comes from its bytes, because it is served back under that type."""

import pytest

from music_engine import covers

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


@pytest.mark.parametrize(
    "data, kind",
    [
        (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "image/jpeg"),
        (PNG, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 20, "image/webp"),
    ],
)
def test_pictures_are_recognised(data, kind):
    assert covers.content_type(data) == kind


@pytest.mark.parametrize(
    "data",
    [b"", b"GIF89a" + b"\x00" * 20, b"<svg onload=alert(1)>", PNG + b"\x00" * covers.MAX_BYTES],
)
def test_anything_else_is_refused(data):
    with pytest.raises(covers.CoverError):
        covers.content_type(data)
