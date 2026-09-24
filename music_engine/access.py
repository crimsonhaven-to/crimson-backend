"""Who may use music. Deny by default, like Lumi: the operator mounts a share
and sets MUSIC_ROOT, then grants accounts one by one on the admin Users tab."""

from fastapi import Depends, HTTPException

from account_engine.deps import require_user
from core.config import get_settings


async def require_music_user(user: dict = Depends(require_user)) -> dict:
    """503 when the server has no library, 403 when this account has no grant,
    so the page can tell a missing setup from a missing permission."""
    if not get_settings().music_root:
        raise HTTPException(status_code=503, detail="Music is not set up on this server.")
    if not user.get("music_enabled"):
        raise HTTPException(
            status_code=403, detail="Music has not been enabled for your account."
        )
    return user
