"""Log formatting: the same lines as before, plus a correlation id.

Replaces the bare ``logging.basicConfig`` that used to sit at the top of
``api.py``. Two formats, selected by ``LOG_FORMAT``:

* ``plain`` (the default): byte-identical to the previous format, with
  ``[req=<id>]`` appended only when the line was emitted while handling a
  request. Startup, scheduler and worker lines are therefore unchanged.
* ``json``: one JSON object per line, for a log pipeline that can query fields.

The default is deliberately the old behaviour. Changing how a running deployment
logs is the kind of thing that quietly breaks somebody's grep, so switching costs
an explicit env var.

The request id itself is minted by ``RequestContextMiddleware`` (see ``api.py``)
and read out of the ContextVar in ``core.observability``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import orjson

from core.observability import current_request_id

# The exact format string api.py's basicConfig used. Kept verbatim so the default
# output is unchanged.
PLAIN_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Attributes the stdlib puts on every LogRecord. Anything NOT in here was added by
# the caller via `extra=` and is worth promoting into the JSON object.
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

    A filter rather than a custom Logger so it applies to records from every
    library in the process (httpx, apscheduler, psycopg) without them knowing
    anything about it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id()
        return True


class PlainFormatter(logging.Formatter):
    """The previous format, with ``[req=<id>]`` appended when one is bound.

    Appended rather than interpolated into the format string so lines emitted
    outside a request (startup, the schedulers, the cache worker) stay exactly as
    they were."""

    def __init__(self) -> None:
        super().__init__(PLAIN_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        rid = getattr(record, "request_id", "")
        return f"{line} [req={rid}]" if rid else line


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Opt-in via ``LOG_FORMAT=json``.

    Uses orjson (already a dependency, and the app's response encoder) and falls
    back to the plain format if a record somehow carries an unserializable
    ``extra`` value, so a bad log call can't take out logging itself."""

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
    """Install the root handler. Drop-in replacement for the previous
    ``logging.basicConfig(level=..., format=...)`` call.

    ``force=True`` mirrors basicConfig's semantics of owning the root handler set.
    uvicorn's own loggers ("uvicorn", "uvicorn.access") carry their own handlers
    with ``propagate=False``, so they are deliberately left alone: reformatting
    the access log is a separate decision from formatting ours."""
    handler = logging.StreamHandler()
    handler.setFormatter(_formatter())
    handler.addFilter(RequestIdFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
