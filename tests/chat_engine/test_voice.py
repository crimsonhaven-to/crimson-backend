"""Lumi's DJ voice: the WAV is found in the response, and any failure is
silence rather than a broken set."""

import asyncio
import base64
import functools

import httpx

from chat_engine import voice

WAV = base64.b64encode(b"\0" * (voice.WAV_HEADER_BYTES + voice.WAV_BYTES_PER_SECOND * 2)).decode()


def _answer(data=WAV):
    return {
        "steps": [
            {"type": "user_input", "content": [{"type": "text", "text": "hi"}]},
            {"type": "model_output", "content": [{"type": "audio", "data": data}]},
        ]
    }


def _speak_against(monkeypatch, handler, key="key"):
    recorded = []
    monkeypatch.setattr(voice, "provider_key", lambda provider: key)
    monkeypatch.setattr(voice, "record_usage", lambda *args: recorded.append(args))
    monkeypatch.setattr(
        voice.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    return asyncio.run(voice.speak(1, "Hello, mortal.")), recorded


def test_the_audio_is_the_last_model_output():
    assert voice.audio_from(_answer()) == WAV
    assert voice.audio_from({"steps": []}) is None
    assert voice.audio_from(["not", "a", "dict"]) is None


def test_usage_is_derived_from_the_length_of_the_wav():
    usage = voice.estimated_usage("Hello, mortal.", WAV)
    assert usage.output_tokens == 2 * voice.AUDIO_TOKENS_PER_SECOND
    assert usage.input_tokens == 4


def test_speech_is_asked_for_in_lumis_voice_and_billed(monkeypatch):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json=_answer())

    audio, recorded = _speak_against(monkeypatch, handler)
    assert audio == WAV
    body = sent[0].read().decode()
    assert voice.VOICE in body and voice.SPEECH_MODEL.model_id in body
    assert sent[0].headers["x-goog-api-key"] == "key"
    assert recorded[0][1] == "gemini"


def test_no_gemini_key_means_no_voice(monkeypatch):
    def handler(request):
        raise AssertionError("must not call out without a key")

    assert _speak_against(monkeypatch, handler, key=None) == (None, [])


def test_a_failing_or_garbled_answer_is_silence(monkeypatch):
    for response in (
        httpx.Response(500, text="down"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=_answer("not base64!")),
    ):
        audio, recorded = _speak_against(monkeypatch, lambda request, r=response: r)
        assert (audio, recorded) == (None, [])


def test_an_unreachable_oracle_is_silence(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route")

    assert _speak_against(monkeypatch, handler) == (None, [])
