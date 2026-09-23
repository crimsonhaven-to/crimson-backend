"""The per-request correlation id every log line carries.

A ContextVar rather than a global, so concurrent requests never see each other's
id, and it follows work that leaves the handler: run_in_threadpool and
create_task both copy the context.
"""

import re
import uuid
from contextvars import ContextVar, Token
from typing import Optional

_request_id: ContextVar[str] = ContextVar("crimson_request_id", default="")

# A reverse proxy may pass the header through, so it is untrusted input bound for
# log lines and a response header: reduced to an opaque token, not sanitized.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")
_MAX_LEN = 64


def new() -> str:
    return uuid.uuid4().hex[:16]


def clean(raw: Optional[str]) -> str:
    """An inbound ``X-Request-ID`` as a safe token, or "" to mint a fresh one."""
    return _UNSAFE.sub("", raw.strip())[:_MAX_LEN] if raw else ""


def bind(value: str) -> Token:
    return _request_id.set(value)


def unbind(token: Token) -> None:
    _request_id.reset(token)


def current() -> str:
    """ "" outside a request, such as at startup or in a scheduler job."""
    return _request_id.get()
