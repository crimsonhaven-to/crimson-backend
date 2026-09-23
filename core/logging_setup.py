"""Log formatting with the per-request correlation id from core.request_id.

``LOG_FORMAT=plain`` (the default) appends ``[req=<id>]`` to lines emitted while
handling a request and leaves every other line alone, so existing greps keep
working. ``LOG_FORMAT=json`` emits one object per line.
"""

from __future__ import annotations

import logging

import orjson

from core import request_id
from core.config import get_settings

PLAIN_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Anything not in here came from a caller's `extra=` and is worth promoting into
# the JSON object.
_STANDARD_ATTRS = frozenset(
    (
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
        "request_id",
    )
)


class RequestIdFilter(logging.Filter):
    """A handler filter rather than a custom Logger, so records from httpx,
    apscheduler and psycopg get the request id too."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id.current()
        return True


class PlainFormatter(logging.Formatter):

    def __init__(self) -> None:
        super().__init__(PLAIN_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        rid = getattr(record, "request_id", "")
        return f"{line} [req={rid}]" if rid else line


class JsonFormatter(logging.Formatter):
    """Falls back to the plain format if a record's ``extra`` cannot be
    serialized, so a bad log call cannot take out logging itself."""

    def __init__(self) -> None:
        super().__init__(PLAIN_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = getattr(record, "request_id", "")
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        try:
            return orjson.dumps(payload, default=str).decode("utf-8")
        except Exception:
            return super().format(record)


def _formatter() -> logging.Formatter:
    return JsonFormatter() if get_settings().log_format == "json" else PlainFormatter()


def configure(level: int = logging.INFO) -> None:
    """Install the root handler. uvicorn's loggers keep their own handlers
    (``propagate=False``), so the access log format is untouched."""
    handler = logging.StreamHandler()
    handler.setFormatter(_formatter())
    handler.addFilter(RequestIdFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
