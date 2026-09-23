"""Signed cache tickets: cache the stream the viewer chose, not the fastest one.

``/watch`` resolves several sources per episode and the fastest usually wins the
race, so enqueueing on resolve would nearly always cache that one. Instead each
cacheable stream carries a ticket, and the player redeems it at
``POST /cache/confirm`` after about ten seconds of watching.

The HMAC means a client can only ask us to cache a stream we resolved ourselves,
never smuggle an arbitrary URL into ffmpeg. Tickets are stateless, so the replica
that mints one need not be the one that redeems it.
"""

from __future__ import annotations

import json
from typing import Optional

from core import signing
from core.media_paths import decode_token, encode_token

_SECRET = signing.resolve_secret("CACHE_TICKET_SECRET")


def mint(
    *,
    url: str,
    type: str,
    source: str,
    language: str,
    tmdb_id: int,
    season_number: int,
    episode_number: int,
    anilist_id: Optional[int],
    media_type: str = "tv",
) -> str:
    """``<payload>.<sig>``. Short keys because the ticket rides in every NDJSON
    stream line."""
    payload = {
        "u": url,
        "t": type,
        "s": source,
        "l": language or "",
        "ti": int(tmdb_id),
        "sn": int(season_number),
        "en": int(episode_number),
        "ai": int(anilist_id) if anilist_id is not None else None,
        "mt": media_type or "tv",
    }
    body = encode_token(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    return f"{body}.{signing.sign(_SECRET, body)}"


def verify(ticket: str) -> Optional[dict]:
    """The minted fields, or None when the ticket is absent, forged or garbled."""
    if not ticket or "." not in ticket:
        return None
    body, _, sig = ticket.rpartition(".")
    if not signing.verify(_SECRET, body, sig):
        return None
    try:
        p = json.loads(decode_token(body) or "")
        return {
            "url": p["u"],
            "type": p["t"],
            "source": p["s"],
            "language": p.get("l") or "",
            "tmdb_id": int(p["ti"]),
            "season_number": int(p["sn"]),
            "episode_number": int(p["en"]),
            "anilist_id": int(p["ai"]) if p.get("ai") is not None else None,
            "media_type": p.get("mt") or "tv",
        }
    except Exception:
        return None
