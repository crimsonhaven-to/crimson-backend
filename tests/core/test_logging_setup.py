import logging

import orjson

from core import logging_setup, request_id
from core.config import get_settings


def _record(msg="hello", **extra):
    record = logging.LogRecord("crimson.test", logging.INFO, __file__, 10, msg, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_plain_format_is_unchanged_outside_a_request():
    """Byte-compatible with the basicConfig it replaced, or somebody's grep breaks."""
    line = logging_setup.PlainFormatter().format(_record())
    assert line.endswith("crimson.test - INFO - hello")
    assert "[req=" not in line


def test_plain_format_appends_the_request_id_when_bound():
    line = logging_setup.PlainFormatter().format(_record(request_id="abc123"))
    assert line.endswith("crimson.test - INFO - hello [req=abc123]")


def test_json_format_carries_the_correlation_fields():
    payload = orjson.loads(logging_setup.JsonFormatter().format(_record(request_id="abc123", tmdb_id=1429)))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "crimson.test"
    assert payload["message"] == "hello"
    assert payload["request_id"] == "abc123"
    # extra= keys become top-level fields, which is the point of JSON logs.
    assert payload["tmdb_id"] == 1429


def test_json_format_survives_an_unserializable_extra():
    class Opaque:
        pass

    assert "hello" in logging_setup.JsonFormatter().format(_record(thing=Opaque()))


def test_request_id_filter_stamps_the_active_id():
    token = request_id.bind("ctx-id")
    try:
        record = _record()
        assert logging_setup.RequestIdFilter().filter(record) is True
        assert record.request_id == "ctx-id"
    finally:
        request_id.unbind(token)


def test_log_format_setting_selects_the_formatter(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "log_format", "json")
    assert isinstance(logging_setup._formatter(), logging_setup.JsonFormatter)
    monkeypatch.setattr(settings, "log_format", "plain")
    assert isinstance(logging_setup._formatter(), logging_setup.PlainFormatter)
