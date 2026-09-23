import pytest

from core import request_id


def test_generated_ids_are_short_and_unique():
    ids = {request_id.new() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 16 for i in ids)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("abc123", "abc123"),
        ("  padded  ", "padded"),
        ("keeps.these:and-under_scores", "keeps.these:and-under_scores"),
        # Reflected into a response header and written to logs, so CR/LF and
        # anything else exotic is dropped.
        ("bad\r\nX-Evil: 1", "badX-Evil:1"),
        ("<script>alert(1)</script>", "scriptalert1script"),
        ("", ""),
        (None, ""),
        # Nothing salvageable means "", so the caller mints a fresh id.
        ("!!!@@@", ""),
    ],
)
def test_clean(raw, expected):
    assert request_id.clean(raw) == expected


def test_clean_truncates():
    assert len(request_id.clean("a" * 500)) == 64


def test_bind_and_unbind_round_trip():
    assert request_id.current() == ""
    token = request_id.bind("deadbeef")
    try:
        assert request_id.current() == "deadbeef"
    finally:
        request_id.unbind(token)
    assert request_id.current() == ""
