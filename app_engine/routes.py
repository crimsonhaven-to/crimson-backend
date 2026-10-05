"""The desktop app's installers and update feed, for members only.

``/app/release`` lists the current release with signed download links for the
website. ``/app/updates/<file>`` is electron-updater's generic feed: the app
sends the member's bearer like every other call. ``/app/download`` serves the
signed links to a plain browser download.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from account_engine.deps import require_user

from . import links, upstream

router = APIRouter(prefix="/app", tags=["desktop app"])

_TYPES = {".json": "application/json", ".yml": "text/yaml", ".yaml": "text/yaml"}


def _media_type(name: str) -> str:
    for suffix, media_type in _TYPES.items():
        if name.endswith(suffix):
            return media_type
    return "application/octet-stream"


def _require_configured() -> None:
    if not upstream.configured():
        raise HTTPException(status_code=503, detail="Desktop app downloads are not configured")


async def _stream(name: str, *, attachment: bool) -> StreamingResponse:
    _require_configured()
    if not upstream.valid_name(name):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        headers, body = await upstream.open_file(name)
    except upstream.NotFound:
        raise HTTPException(status_code=404, detail="Not found")
    except upstream.UpstreamError:
        raise HTTPException(status_code=502, detail="Release storage is unreachable")
    # The feed's latest*.yml changes with every release, so nothing is cached.
    headers["Cache-Control"] = "private, no-store"
    if attachment:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return StreamingResponse(body, media_type=_media_type(name), headers=headers)


@router.get("/release")
async def release(_user: dict = Depends(require_user)):
    _require_configured()
    try:
        data = await upstream.manifest()
    except upstream.UpstreamError:
        raise HTTPException(status_code=502, detail="Release storage is unreachable")
    files = [
        {**f, "url": links.signed_path(f["name"])}
        for f in data["files"]
        if isinstance(f, dict) and upstream.valid_name(str(f.get("name", "")))
    ]
    return {"version": data.get("version"), "released_at": data.get("released_at"), "files": files}


@router.get("/updates/{name}")
async def update_file(name: str, _user: dict = Depends(require_user)):
    return await _stream(name, attachment=False)


@router.get("/download/{name}")
async def download(name: str, e: int = Query(...), s: str = Query(...)):
    if not links.verify(name, e, s):
        raise HTTPException(status_code=404, detail="Not found")
    return await _stream(name, attachment=True)
