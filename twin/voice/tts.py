"""Text to speech via a cloud TTS API.

Like STT, this sits outside the agent loop -- text in, audio bytes out.

The one piece of real logic here is `for_speech()`. The agent's replies are
written for a terminal and routinely contain markdown and URLs; reading those
aloud verbatim is unbearable, especially for opportunity nudges, which are
mostly links.
"""

import re
from typing import Optional

import httpx

from twin.config import SETTINGS
from twin.voice import audio

SPEECH_URL = "https://api.openai.com/v1/audio/speech"
TIMEOUT_SECONDS = 90.0

# The API's own input ceiling.
MAX_SPEECH_CHARS = 4096


class SynthesisError(RuntimeError):
    """Speech synthesis failed or isn't configured."""


def for_speech(text: str, max_chars: int = MAX_SPEECH_CHARS) -> str:
    """Flatten terminal-formatted text into something worth hearing."""
    if not text:
        return ""

    spoken = text

    # Markdown links: keep the label, drop the target.
    spoken = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", spoken)
    # Bare URLs: nobody wants "h t t p s colon slash slash" read out.
    spoken = re.sub(r"https?://\S+", "the link I've put on screen", spoken)
    # Code fences and inline code markers.
    spoken = re.sub(r"```[^`]*```", " ", spoken, flags=re.S)
    spoken = spoken.replace("`", "")
    # Emphasis and heading markers.
    spoken = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", spoken)
    spoken = re.sub(r"^#{1,6}\s*", "", spoken, flags=re.M)
    # List bullets become sentence breaks so they don't run together.
    spoken = re.sub(r"^\s*[-*+]\s+", "", spoken, flags=re.M)

    spoken = re.sub(r"[ \t]{2,}", " ", spoken)
    spoken = re.sub(r"\n{2,}", "\n", spoken).strip()

    if len(spoken) > max_chars:
        # Cut at a sentence boundary where possible rather than mid-word.
        clipped = spoken[:max_chars]
        boundary = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "))
        spoken = clipped[: boundary + 1] if boundary > max_chars // 2 else clipped
    return spoken


def synthesize(text: str, voice: Optional[str] = None) -> bytes:
    """Render text as mp3 audio."""
    if not SETTINGS.voice_enabled:
        raise SynthesisError("OPENAI_API_KEY is not set -- text-to-speech is unavailable.")

    spoken = for_speech(text)
    if not spoken:
        return b""

    try:
        response = httpx.post(
            SPEECH_URL,
            headers={
                "Authorization": "Bearer {0}".format(SETTINGS.openai_api_key),
                "Content-Type": "application/json",
            },
            json={
                "model": SETTINGS.tts_model,
                "input": spoken,
                "voice": voice or SETTINGS.tts_voice,
                "response_format": "mp3",
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SynthesisError("Speech synthesis failed: {0}".format(exc)) from exc

    return response.content


def speak(text: str, voice: Optional[str] = None) -> None:
    """Synthesize and play. Raises SynthesisError; playback errors propagate."""
    clip = synthesize(text, voice)
    if clip:
        audio.play(clip, suffix=".mp3")
