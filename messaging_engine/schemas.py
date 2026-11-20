"""Request bodies for the messaging routes."""

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field

MAX_BODY = 2000
MAX_GROUP_NAME = 60
# Big enough for a friend group, small enough that one bad add is not a broadcast.
MAX_GROUP_MEMBERS = 32

Handle = Annotated[str, Field(min_length=1, max_length=40)]


class SongAttachment(BaseModel):
    kind: Literal["song"]
    token: str = Field(..., max_length=200)


class TitleAttachment(BaseModel):
    kind: Literal["title"]
    media: Literal["anime", "show", "movie", "manga"]
    id: int = Field(..., ge=1)
    title: str = Field(..., min_length=1, max_length=300)
    poster: Optional[str] = Field(None, max_length=1000)


Attachment = Annotated[Union[SongAttachment, TitleAttachment], Field(discriminator="kind")]


class DirectIn(BaseModel):
    handle: Handle


class GroupIn(BaseModel):
    name: str = Field(..., max_length=MAX_GROUP_NAME)
    handles: List[Handle] = Field(..., min_length=1, max_length=MAX_GROUP_MEMBERS - 1)


class RenameIn(BaseModel):
    name: str = Field(..., max_length=MAX_GROUP_NAME)


class AddMembersIn(BaseModel):
    handles: List[Handle] = Field(..., min_length=1, max_length=MAX_GROUP_MEMBERS - 1)


class MessageIn(BaseModel):
    body: str = Field("", max_length=MAX_BODY)
    attachment: Optional[Attachment] = None


class ReadIn(BaseModel):
    last_id: int = Field(..., ge=0)
