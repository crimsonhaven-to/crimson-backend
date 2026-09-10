"""Log formatting, with a per-request correlation id.

Two formats, chosen by ``LOG_FORMAT``:

* ``plain`` (default) appends ``[req=<id>]`` only on lines emitted while handling
  a request, so startup, scheduler and worker lines are untouched.
* ``json`` emits one object per line for a pipeline that can query fields.

Plain is the default because changing how a running deployment logs quietly
breaks somebody's grep, so switching costs an explicit env var.

The id is minted by ``RequestContextMiddleware`` and read from the ContextVar in
core.observability.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import orjson

from core.observability import current_request_id

# Verbatim from the basicConfig this replaced, so default output is unchanged.
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
    """Stamp the active request id onto every record.

    A filter rather than a custom Logger, so it also covers records from httpx,
    apscheduler and psycopg without them knowing anything about it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id()
        return True


class PlainFormatter(logging.Formatter):
    """The previous format, with ``[req=<id>]`` appended when one is bound.

    Appended rather than interpolated, so lines emitted outside a request stay
    exactly as they were."""

    def __init__(self) -> None:
        super().__init__(PLAIN_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        rid = getattr(record, "request_id", "")
        return f"{line} [req={rid}]" if rid else line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, opt-in via ``LOG_FORMAT=json``.

    Falls back to the plain format if a record carries an unserializable
    ``extra``, so a bad log call can't take out logging itself."""

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


def _formatter(name: Optional[str] = None) -> logging.Formatter:
    fmt = (name if name is not None else os.getenv("LOG_FORMAT", "plain")).strip().lower()
    return JsonFormatter() if fmt == "json" else PlainFormatter()


def configure(level: int = logging.INFO) -> None:
    """Install the root handler.

    ``force=True`` mirrors basicConfig owning the root handler set. uvicorn's own
    loggers carry their own handlers with ``propagate=False`` and are left alone:
    reformatting the access log is a separate decision from formatting ours."""
    handler = logging.StreamHandler()
    handler.setFormatter(_formatter())
    handler.addFilter(RequestIdFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
