"""The request dependencies that resolve who is calling."""

from typing import Optional

from fastapi import Depends, Header, HTTPException

from .db import store


def parse_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip() or None


def bearer_token(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """The raw session token, or None. ``require_user`` owns rejecting it."""
    return parse_bearer(authorization)


def require_user(token: Optional[str] = Depends(bearer_token)) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    user = store.get_user_by_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
