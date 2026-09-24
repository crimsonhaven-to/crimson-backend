from typing import Literal

from pydantic import BaseModel, Field


class SpotifyConnect(BaseModel):
    """The browser's half of Authorization Code with PKCE: it held the verifier
    across the redirect and now hands the code over once."""

    client_id: str = Field(..., pattern=r"^[0-9a-fA-F]{32}$")
    code: str = Field(..., min_length=1, max_length=2048)
    code_verifier: str = Field(..., min_length=43, max_length=128)
    redirect_uri: str = Field(..., pattern=r"^https://|^http://127\.0\.0\.1[:/]", max_length=500)


class PlaylistImport(BaseModel):
    source: Literal["spotify", "public"]
    # A share link, a spotify:playlist: URI, a bare id, or "liked".
    playlist: str = Field(..., min_length=1, max_length=500)


class CsvImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    csv: str = Field(..., min_length=1, max_length=5_000_000)


class PlaylistUpdate(BaseModel):
    sync_enabled: bool


class MatchChoice(BaseModel):
    url: str = Field(..., pattern=r"^https://", max_length=500)
