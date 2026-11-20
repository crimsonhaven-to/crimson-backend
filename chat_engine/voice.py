"""Lumi's speaking voice for the DJ intros, through Gemini's speech model.

Not part of providers.py: that module is the chat interface, streamed text and
tool calls, while speech is one unary request with a WAV file back. It always
needs a Gemini key, whichever provider the chat runs on. Without one, or when the
call fails, the intro is only shown, never spoken: a voice is a nicety and must
not hold up the music.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Dict, Optional

import httpx

from .access import provider_key, record_usage
from .models import GEMINI, ChatModel
from .providers import Usage

logger = logging.getLogger("crimson.chat.voice")

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
# Google's rates through 2026-12-31; both double from 2027. They only feed the
# dashboard's estimate, like the prices in models.py.
SPEECH_MODEL = ChatModel(
    "gemini-3.8-flash-tts", GEMINI, "Gemini 3.8 Flash TTS", 0.50, 9.00, 0.0
)
VOICE = "Kore"
STYLE = "theatrical and warm, like a late night radio host"
# An intro is two sentences, so a slow answer is a broken one: the set must not wait.
TIMEOUT_SECONDS = 20.0

# The response carries no usage counts, so they are derived from what is
# documented: 25 audio tokens a second, and a WAV of 24 kHz mono 16-bit PCM.
AUDIO_TOKENS_PER_SECOND = 25
WAV_BYTES_PER_SECOND = 24_000 * 2
WAV_HEADER_BYTES = 44
CHARS_PER_TEXT_TOKEN = 4


def _request(text: str) -> Dict:
    return {
        "model": SPEECH_MODEL.model_id,
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [{"type": "speech_metadata", "style": STYLE}],
                    }
                ],
            }
        ],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": VOICE}]},
    }


def audio_from(answer: Dict) -> Optional[str]:
    """The base64 WAV in an interactions response, or None when it holds none."""
    if not isinstance(answer, dict):
        return None
    found = None
    for step in answer.get("steps") or []:
        if step.get("type") != "model_output":
            continue
        for item in step.get("content") or []:
            if item.get("type") == "audio" and item.get("data"):
                found = item["data"]
    return found


def estimated_usage(text: str, audio: str) -> Usage:
    wav_bytes = len(audio) * 3 // 4
    seconds = max(0, wav_bytes - WAV_HEADER_BYTES) / WAV_BYTES_PER_SECOND
    return Usage(
        input_tokens=-(-len(text) // CHARS_PER_TEXT_TOKEN),
        output_tokens=round(seconds * AUDIO_TOKENS_PER_SECOND),
    )


async def speak(user_id: int, text: str) -> Optional[str]:
    """Lumi saying ``text``, as a base64 WAV, or None when she cannot."""
    api_key = provider_key(GEMINI)
    if not api_key or not text:
        return None
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(ENDPOINT, headers=headers, json=_request(text))
    except httpx.HTTPError as exc:
        logger.warning("dj voice for user %s failed: %s", user_id, exc)
        return None
    if response.status_code >= 400:
        logger.warning("dj voice failed: %s %s", response.status_code, response.text[:400])
        return None
    try:
        audio = audio_from(response.json())
        base64.b64decode(audio or "", validate=True)
    except ValueError:
        logger.warning("dj voice answered something that is not audio")
        return None
    if audio:
        await asyncio.to_thread(
            record_usage, user_id, GEMINI, SPEECH_MODEL, estimated_usage(text, audio)
        )
    return audio
