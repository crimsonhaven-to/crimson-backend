"""Who may message: any signed-in member who has picked a handle."""

from fastapi import Depends, HTTPException

from account_engine.deps import require_user


def require_member(user: dict = Depends(require_user)) -> dict:
    # 409, not 403: the client answers it by asking for a handle, not by hiding chat.
    if not user.get("handle"):
        raise HTTPException(status_code=409, detail="Pick a handle before messaging")
    return user
