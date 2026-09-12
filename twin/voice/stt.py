"""Speech to text via the Whisper API.

Sits outside the agent's tool-calling loop (spec section 9): it takes audio and
returns a string, nothing more. Swapping in a local Whisper model means
reimplementing `transcribe` and touching nothing else.
"""

from typing import Optional

import httpx

from twin.config import SETTINGS

TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
TIMEOUT_SECONDS = 90.0


class TranscriptionError(RuntimeError):
    """Transcription failed or isn't configured."""


def transcribe(wav_bytes: bytes, language: Optional[str] = None) -> str:
    """Transcribe WAV audio to text."""
    if not SETTINGS.voice_enabled:
        raise TranscriptionError(
            "OPENAI_API_KEY is not set -- speech-to-text is unavailable."
        )
    if not wav_bytes:
        return ""

    data = {"model": SETTINGS.stt_model}
    if language:
        data["language"] = language

    try:
        response = httpx.post(
            TRANSCRIPTION_URL,
            headers={"Authorization": "Bearer {0}".format(SETTINGS.openai_api_key)},
            files={"file": ("speech.wav", wav_bytes, "audio/wav")},
            data=data,
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TranscriptionError("Transcription request failed: {0}".format(exc)) from exc

    return (response.json().get("text") or "").strip()
