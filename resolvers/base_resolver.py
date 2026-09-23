from typing import Any


class BaseResolver:
    """Turns a scraper's embed into something the player can load.

    ``resolve_streams`` picks the first resolver whose ``domain_keyword`` is a
    substring of the embed. Operator sources emit a ``crimson-<name>:<token>``
    marker, so the embed is a routing token rather than a third-party URL.

    ``resolve`` returns one of:

    | Shape | Meaning |
    | --- | --- |
    | ``None`` | nothing playable, the source is dropped |
    | ``str`` | a stream URL; a leading ``/`` is a backend path made absolute, ``.m3u8`` plays as HLS, anything else as MP4 |
    | ``dict`` | ``{"url", "source"?, "type"?, "subtitles"?}`` to set the label or attach subtitles per stream |
    | ``list[dict]`` | one embed fanned out into several tiles (qualities, languages) |

    ``resolve_direct`` serves the ``/resolve`` grant, where the client fetches the
    bytes itself: a list of ``{"url", "streamType", "label"?, "headers"?,
    "subtitles"?}`` with raw upstream URLs. Only sources wired into a grant
    implement it.

    An overlay module opts out of the ``/watch`` registries with ``RESOLVE_ONLY =
    True`` and wires into ``/resolve`` with a ``RESOLVE_GRANT`` descriptor (see
    ``core.private_sources.discover_resolve_grants``).
    """

    domain_keyword: str = ""
    source_name: str = ""

    async def resolve(self, embed_url: str) -> str | dict | list[dict] | None:
        raise NotImplementedError

    async def resolve_direct(self, embed_url: str) -> list[dict[str, Any]] | None:
        raise NotImplementedError
