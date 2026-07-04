"""Manga surface — the reading counterpart of the video sources.

A fourth, additive content surface (after anime, non-anime shows and movies): the
same shape of pipeline (id-keyed discovery → source fan-out → an optional signed
same-origin proxy) but the *unit* is a chapter of page images, not a stream, so it
lives in its own engine instead of bending the /watch NDJSON contract.

Discovery + metadata come from AniList (its ``MediaType`` already includes
``MANGA``), exactly mirroring how the anime surface uses AniList — so covers,
genres and ids are consistent with the rest of the site and manga favorites /
recommendations share the AniList id space.

Like the video sources, the public backend is a **metadata + orchestration** layer
only: it never talks to a manga host. The chapter list and page images are resolved
in the viewer's own browser by ``crimson-sources`` (E2/E3) and merged client-side.
An operator build may inject a private ``MangaProvider`` (see ``provider.py``) to
resolve those server-side for extension-less devices; a base build has none, so the
backend reports "unmapped" and the client fills it in.

Public surface:
  * ``GET /trending/manga``          — AniList trending manga (discovery row)
  * ``GET /search/manga``            — AniList manga search (unified search)
  * ``GET /manga-overview/{id}``     — AniList metadata + candidate titles + (with a
                                       provider) the mapped chapter list
  * ``GET /read/{id}/{chapter_id}``  — one chapter's ordered page images (provider
                                       builds only; else resolved client-side)
  * ``GET /manga_proxy``             — optional signed image relay (provider builds
                                       only; PUBLIC + HMAC-signed like
                                       ``/subtitles_proxy``)
"""

from manga_engine.routes import router as manga_router

__all__ = ["manga_router"]
