"""Microphone capture and audio playback.

Deliberately knows nothing about the agent or about transcription -- it moves
bytes to and from the sound hardware. Spec section 9 keeps the I/O layer
outside the agent loop so either half can be swapped for a local model without
touching agent logic; this module is the hardware end of that boundary.
"""

import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import wave
from typing import List, Optional

import numpy as np

# 16 kHz mono is what Whisper downsamples to anyway, so capturing at a higher
# rate only inflates the upload.
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # int16

# Guards against a forgotten open mic turning into a huge upload.
MAX_RECORDING_SECONDS = 120

# Below these, treat the capture as "they didn't actually say anything" and
# skip the API call rather than transcribing silence.
MIN_RECORDING_SECONDS = 0.4
SILENCE_RMS_THRESHOLD = 120.0  # int16 units; room tone sits well under this


class VoiceUnavailable(RuntimeError):
    """Audio hardware or its driver isn't usable."""


class PlaybackUnavailable(RuntimeError):
    """No usable audio player on this machine."""


def to_wav_bytes(
    samples: np.ndarray,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
) -> bytes:
    """Wrap raw int16 PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.astype(np.int16).tobytes())
    return buffer.getvalue()


def is_silence(
    samples: np.ndarray,
    sample_rate: int = SAMPLE_RATE,
    rms_threshold: float = SILENCE_RMS_THRESHOLD,
) -> bool:
    """Whether a capture is too short or too quiet to be worth transcribing."""
    if samples.size == 0:
        return True
    if samples.size / float(sample_rate) < MIN_RECORDING_SECONDS:
        return True
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    return rms < rms_threshold


def _import_sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        # OSError covers a present package with a missing PortAudio library,
        # which is the usual Linux failure.
        raise VoiceUnavailable(
            "Microphone capture needs `sounddevice` and PortAudio. "
            "Install with `pip install sounddevice` (on Linux also "
            "`apt install libportaudio2`)."
        ) from exc
    return sounddevice


def has_input_device() -> bool:
    try:
        sounddevice = _import_sounddevice()
        return any(
            device["max_input_channels"] > 0 for device in sounddevice.query_devices()
        )
    except Exception:
        return False


def record_until(stop_signal, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Capture from the default mic until `stop_signal()` returns.

    `stop_signal` is a blocking callable -- in the CLI it waits on Enter.
    Passing it in keeps this function free of any opinion about how the user
    signals that they're done.
    """
    sounddevice = _import_sounddevice()

    chunks: List[np.ndarray] = []
    captured_frames = 0
    frame_limit = MAX_RECORDING_SECONDS * sample_rate

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        nonlocal captured_frames
        if captured_frames >= frame_limit:
            return
        chunks.append(indata.copy())
        captured_frames += frames

    try:
        with sounddevice.InputStream(
            samplerate=sample_rate,
            channels=CHANNELS,
            dtype="int16",
            callback=callback,
        ):
            stop_signal()
    except VoiceUnavailable:
        raise
    except Exception as exc:
        raise VoiceUnavailable(
            "Could not open the microphone: {0}. On macOS, grant microphone "
            "access to your terminal in System Settings > Privacy & Security.".format(exc)
        ) from exc

    if not chunks:
        return np.zeros(0, dtype=np.int16)

    captured = np.concatenate(chunks, axis=0).reshape(-1)
    return captured[: int(frame_limit)]


def _player_command(path: str) -> Optional[List[str]]:
    """Pick an audio player for this platform, or None if there isn't one."""
    system = platform.system()

    if system == "Darwin" and os.path.exists("/usr/bin/afplay"):
        # Built in, and handles mp3 without any extra dependency.
        return ["/usr/bin/afplay", path]

    ffplay = shutil.which("ffplay")
    if ffplay:
        return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", path]

    if system == "Linux":
        for player in ("mpg123", "aplay", "paplay"):
            found = shutil.which(player)
            if found:
                return [found, path]

    return None


def play(audio: bytes, suffix: str = ".mp3") -> None:
    """Play an encoded audio clip through the system's default output."""
    if not audio:
        return

    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(audio)
        handle.close()

        if platform.system() == "Windows":
            # winsound only handles WAV; anything else goes to the shell
            # association, which is the most reliable no-dependency route.
            os.startfile(handle.name)  # type: ignore[attr-defined]  # noqa: S606
            return

        command = _player_command(handle.name)
        if command is None:
            raise PlaybackUnavailable(
                "No audio player found. Install ffmpeg (`brew install ffmpeg`) "
                "or run in text mode."
            )
        subprocess.run(command, check=True, capture_output=True)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def wait_for_enter(prompt: str = "") -> None:
    """Block until the user presses Enter. The default push-to-talk stop signal."""
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    try:
        input()
    except EOFError:
        pass
