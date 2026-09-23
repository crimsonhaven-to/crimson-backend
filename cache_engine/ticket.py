"""
Signed cache tickets — defer a stream's caching until the player confirms the
viewer actually watched it.

Why this exists: ``/watch`` resolves several sources per episode, fastest-first
(PlayIMDb usually wins the race). If we enqueued a download the instant a stream
resolved, we'd almost always cache that fastest source — not the high-quality,
language-tagged one (Voe / VidSrc / …) the viewer actually ends up choosing. So
instead the watch path stamps each *cacheable* stream with an opaque ticket; the
player calls ``POST /cache/confirm`` with that ticket once the viewer has watched
~10s, and only then does the download get enqueued.

The ticket is HMAC-signed with the same shared ``PROXY_SECRET`` the proxies use,
so a client can only ever ask us to cache a stream **we ourselves resolved and
signed** — it can't smuggle an arbitrary URL into ffmpeg (closes the SSRF hole,
exactly like the signed stream proxies). Tickets are self-contained and stateless
so the ``/watch`` replica that mints one and the ``/cache/confirm`` replica that
redeems it needn't be the same node.
"""

from __future__ import annotations

import base64
import json
from typing import Optional

from core import signing

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
    """A compact ``<payload>.<sig>`` ticket carrying everything ``maybe_enqueue``
    needs to reconstruct this exact stream. Short keys keep it small, since it rides
    in every NDJSON stream line. TMDB numbers movies and shows independently, so
    ``media_type`` keeps a movie from colliding with a same-id show."""
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
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{body}.{signing.sign(_SECRET, body)}"


def verify(ticket: str) -> Optional[dict]:
    """Decode a ticket minted by :func:`mint`, or None if it's absent, forged or
    garbled. The returned dict is shaped for ``DownloadManager.maybe_enqueue``."""
    if not ticket or "." not in ticket:
        return None
    body, _, sig = ticket.rpartition(".")
    if not signing.verify(_SECRET, body, sig):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        p = json.loads(base64.urlsafe_b64decode(body + pad))
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
