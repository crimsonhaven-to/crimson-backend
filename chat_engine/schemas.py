from typing import Optional

from pydantic import BaseModel, Field

# Long enough for a real question, short enough that one message cannot become
# a large bill.
MAX_MESSAGE_CHARS = 2000


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: Optional[int] = None


class ChatSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    provider: Optional[str] = Field(None, pattern="^(anthropic|gemini)$")
    model: Optional[str] = None
    monthly_token_budget: Optional[int] = Field(None, ge=0)
    history_turns: Optional[int] = Field(None, ge=1, le=50)
    max_tool_iterations: Optional[int] = Field(None, ge=1, le=10)
