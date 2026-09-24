"""Request and response bodies for the account routes."""

from typing import Optional

from pydantic import BaseModel, Field, model_validator

from . import passwords

MAX_USERNAME_LENGTH = 20


# --- mnemonic sign-in -------------------------------------------------------
class ChallengeRequest(BaseModel):
    public_key: str


class ChallengeResponse(BaseModel):
    public_key: str
    challenge: str
    expires_at: str


class RegisterRequest(BaseModel):
    public_key: str
    challenge: str
    signature: str
    # Required, so a freshly minted keypair cannot bypass the invite gate.
    invite_code: str
    label: Optional[str] = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    public_key: str
    challenge: str
    signature: str


class AuthResponse(BaseModel):
    public_key: str
    label: Optional[str]
    session_token: str
    expires_at: str
    created: bool


# --- email sign-in ----------------------------------------------------------
class EmailRegisterRequest(BaseModel):
    email: str
    password: str
    invite_code: str
    label: Optional[str] = Field(default=None, max_length=100)


class EmailLoginRequest(BaseModel):
    email: str
    password: str


class EmailTokenRequest(BaseModel):
    token: str


class EmailOnlyRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


# --- profile ------------------------------------------------------------------
class UsernameIn(BaseModel):
    # Empty or null clears it, falling back to a generic greeting.
    username: Optional[str] = Field(default=None, max_length=MAX_USERNAME_LENGTH)


class DeleteAccountRequest(BaseModel):
    """Proof that the caller is the account holder and not a stolen token: the
    password for an email account, a signed challenge for a mnemonic one."""

    password: Optional[str] = Field(None, max_length=passwords.MAX_PASSWORD_LENGTH)
    challenge: Optional[str] = None
    signature: Optional[str] = None


# --- library ------------------------------------------------------------------
class FavoriteIn(BaseModel):
    tmdb_id: Optional[int] = None
    anilist_id: Optional[int] = None
    season_number: Optional[int] = None
    media_type: Optional[str] = None
    title: Optional[str] = None
    poster: Optional[str] = None
    list_name: str = Field(default="favorites", min_length=1, max_length=100)

    @model_validator(mode="after")
    def _need_an_id(self):
        if self.tmdb_id is None and self.anilist_id is None:
            raise ValueError("Provide at least one of tmdb_id or anilist_id")
        return self


class ProgressIn(BaseModel):
    tmdb_id: Optional[int] = None
    anilist_id: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    position_seconds: Optional[float] = None
    duration_seconds: Optional[float] = None
    # 'in_progress' or 'completed'; inferred from the position when omitted.
    status: Optional[str] = None
    title: Optional[str] = None
    poster: Optional[str] = None
    # 'movie' and 'manga' get their own key namespaces; 'local' is on-disk media
    # identified by local_id, its path token.
    media_type: Optional[str] = None
    local_id: Optional[str] = None

    @model_validator(mode="after")
    def _need_an_id(self):
        if self.tmdb_id is None and self.anilist_id is None and self.local_id is None:
            raise ValueError("Provide at least one of tmdb_id, anilist_id or local_id")
        return self


# --- admin --------------------------------------------------------------------
class UserUpdate(BaseModel):
    is_admin: Optional[bool] = None
    email_verified: Optional[bool] = None
    chat_enabled: Optional[bool] = None
    music_enabled: Optional[bool] = None
    # Absent leaves the budget alone, a number sets it, and 0 freezes the user
    # without revoking access. Clearing it back to the global default needs its
    # own flag, because JSON null and an omitted field look the same here.
    chat_monthly_token_budget: Optional[int] = Field(None, ge=0)
    chat_budget_reset: Optional[bool] = None


class BroadcastEmail(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=20000)
    # Unverified addresses may not belong to the account holder.
    verified_only: bool = True


class InviteCreate(BaseModel):
    count: int = Field(1, ge=1, le=50)
    ttl_hours: Optional[int] = Field(None, ge=1, le=8760)
