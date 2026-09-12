"""Voice layer: WAV assembly, silence gating, speech cleanup, session wiring.

No real audio hardware and no API calls -- the I/O boundary is injected, which
is the point of keeping STT/TTS outside the agent loop.
"""

import contextlib
import wave

import io as _io
import numpy as np
import pytest

from twin.config import SETTINGS
from twin.voice import audio, session, stt, tts


# --- fakes ----------------------------------------------------------------

class FakeConsole:
    def __init__(self, inputs=()):
        self.inputs = list(inputs)
        self.printed = []

    def print(self, *args, **kwargs):
        self.printed.append(" ".join(str(a) for a in args))

    def input(self, prompt=""):
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def status(self, *args, **kwargs):
        return contextlib.nullcontext()

    def saw(self, needle):
        return any(needle in line for line in self.printed)


class FakeAgent:
    def __init__(self, reply="acknowledged"):
        self.reply = reply
        self.received = []

    def send(self, message):
        self.received.append(message)
        return self.reply


@pytest.fixture(autouse=True)
def no_queued_nudges(monkeypatch):
    """Keep session tests off the real database.

    The opening-nudge path has its own test below; everywhere else an empty
    queue keeps assertions about what reached the agent unambiguous.
    """
    monkeypatch.setattr(session.store, "pending_nudges", lambda: [])


def tone(seconds=1.0, amplitude=5000, sample_rate=audio.SAMPLE_RATE):
    t = np.linspace(0, seconds, int(sample_rate * seconds), endpoint=False)
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.int16)


def make_io(samples=None, transcript="hello there", speak=None, silence=None):
    return session.VoiceIO(
        record=lambda: tone() if samples is None else samples,
        transcribe=lambda wav: transcript,
        speak=speak or (lambda text: None),
        is_silence=silence or audio.is_silence,
    )


# --- WAV assembly ---------------------------------------------------------

def test_to_wav_bytes_produces_a_readable_wav():
    samples = tone(0.5)
    data = audio.to_wav_bytes(samples)

    with wave.open(_io.BytesIO(data), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == audio.SAMPLE_RATE
        assert handle.getnframes() == samples.size


def test_to_wav_bytes_handles_empty_input():
    data = audio.to_wav_bytes(np.zeros(0, dtype=np.int16))
    with wave.open(_io.BytesIO(data), "rb") as handle:
        assert handle.getnframes() == 0


# --- silence gating -------------------------------------------------------

def test_silence_detected_for_empty_capture():
    assert audio.is_silence(np.zeros(0, dtype=np.int16)) is True


def test_silence_detected_for_too_short_capture():
    """A stray double-Enter shouldn't cost an API call."""
    assert audio.is_silence(tone(0.1)) is True


def test_silence_detected_for_quiet_room_tone():
    quiet = (np.random.RandomState(0).normal(0, 20, audio.SAMPLE_RATE)).astype(np.int16)
    assert audio.is_silence(quiet) is True


def test_real_speech_is_not_silence():
    assert audio.is_silence(tone(1.0)) is False


# --- playback selection ---------------------------------------------------

def test_player_prefers_afplay_on_macos(monkeypatch):
    monkeypatch.setattr(audio.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(audio.os.path, "exists", lambda p: p == "/usr/bin/afplay")
    assert audio._player_command("/tmp/a.mp3")[0] == "/usr/bin/afplay"


def test_player_falls_back_to_ffplay(monkeypatch):
    monkeypatch.setattr(audio.platform, "system", lambda: "Linux")
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/ffplay" if name == "ffplay" else None)
    command = audio._player_command("/tmp/a.mp3")
    assert command[0] == "/usr/bin/ffplay"
    assert "-autoexit" in command


def test_player_returns_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(audio.platform, "system", lambda: "Linux")
    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    assert audio._player_command("/tmp/a.mp3") is None


def test_play_ignores_empty_audio():
    audio.play(b"")  # must not raise or spawn a player


# --- speech cleanup -------------------------------------------------------

def test_for_speech_keeps_link_label_drops_target():
    assert tts.for_speech("see [the hackathon](https://x.test/1)") == "see the hackathon"


def test_for_speech_replaces_bare_urls():
    spoken = tts.for_speech("register at https://x.test/signup today")
    assert "https" not in spoken
    assert "link" in spoken


def test_for_speech_strips_markdown_emphasis_and_headings():
    assert tts.for_speech("## **Heads up**") == "Heads up"


def test_for_speech_strips_code_markers():
    assert tts.for_speech("run `pytest` now") == "run pytest now"


def test_for_speech_removes_list_bullets():
    assert "-" not in tts.for_speech("- first\n- second")


def test_for_speech_truncates_at_a_sentence_boundary():
    text = ("This is a sentence. " * 40).strip()
    spoken = tts.for_speech(text, max_chars=100)
    assert len(spoken) <= 100
    assert spoken.endswith(".")


def test_for_speech_handles_empty():
    assert tts.for_speech("") == ""


# --- configuration gating -------------------------------------------------

def test_transcribe_without_key_raises(monkeypatch):
    monkeypatch.setattr(SETTINGS, "openai_api_key", None)
    with pytest.raises(stt.TranscriptionError):
        stt.transcribe(b"audio")


def test_synthesize_without_key_raises(monkeypatch):
    monkeypatch.setattr(SETTINGS, "openai_api_key", None)
    with pytest.raises(tts.SynthesisError):
        tts.synthesize("hello")


def test_transcribe_with_empty_audio_skips_the_call(monkeypatch):
    monkeypatch.setattr(SETTINGS, "openai_api_key", "stub")
    monkeypatch.setattr(stt.httpx, "post", lambda *a, **k: pytest.fail("called API"))
    assert stt.transcribe(b"") == ""


# --- exit phrases ---------------------------------------------------------

@pytest.mark.parametrize("phrase", ["quit", "Quit.", "goodbye", "Bye!", "that's all"])
def test_spoken_exit_phrases_recognized(phrase):
    assert session.is_exit_phrase(phrase) is True


def test_ordinary_speech_is_not_an_exit_phrase():
    assert session.is_exit_phrase("what's on my calendar") is False


# --- capture_utterance ----------------------------------------------------

def test_capture_returns_none_on_silence():
    console = FakeConsole()
    io = make_io(samples=np.zeros(0, dtype=np.int16))
    assert session.capture_utterance(io, console) is None
    assert console.saw("didn't catch anything")


def test_capture_returns_none_on_empty_transcript():
    console = FakeConsole()
    io = make_io(transcript="   ")
    assert session.capture_utterance(io, console) is None


def test_capture_reports_transcription_failure_without_raising():
    console = FakeConsole()

    def boom(wav):
        raise stt.TranscriptionError("whisper down")

    io = session.VoiceIO(
        record=lambda: tone(), transcribe=boom,
        speak=lambda t: None, is_silence=audio.is_silence,
    )
    assert session.capture_utterance(io, console) is None
    assert console.saw("whisper down")


def test_capture_returns_transcript():
    io = make_io(transcript="what's due tomorrow")
    assert session.capture_utterance(io, FakeConsole()) == "what's due tomorrow"


# --- session loop ---------------------------------------------------------

def test_typed_input_bypasses_the_microphone():
    """Typing mid-session should not record audio."""
    def no_record():
        raise AssertionError("recorded despite typed input")

    io = session.VoiceIO(
        record=no_record, transcribe=lambda w: "",
        speak=lambda t: None, is_silence=audio.is_silence,
    )
    agent = FakeAgent()
    console = FakeConsole(inputs=["what's on today", "q"])

    session.run_voice_session(agent, console, io=io)
    assert agent.received == ["what's on today"]


def test_spoken_turn_reaches_the_agent_and_is_spoken_back():
    spoken = []
    io = make_io(transcript="what's due", speak=spoken.append)
    agent = FakeAgent(reply="two things are due")
    console = FakeConsole(inputs=["", "q"])

    session.run_voice_session(agent, console, io=io)

    assert agent.received == ["what's due"]
    assert spoken == ["two things are due"]


def test_spoken_exit_phrase_ends_the_session():
    io = make_io(transcript="goodbye")
    agent = FakeAgent()
    console = FakeConsole(inputs=[""])

    assert session.run_voice_session(agent, console, io=io) == 0
    assert agent.received == [], "sent the exit phrase to the agent"


def test_tts_failure_degrades_to_text_and_keeps_going():
    """A broken speaker must not drop the user out of the conversation."""
    calls = []

    def failing_speak(text):
        calls.append(text)
        raise tts.SynthesisError("tts down")

    io = make_io(transcript="hello", speak=failing_speak)
    agent = FakeAgent(reply="hi back")
    console = FakeConsole(inputs=["", "", "q"])

    assert session.run_voice_session(agent, console, io=io) == 0
    # Tried once, gave up, and stopped retrying on the second turn.
    assert len(calls) == 1
    assert console.saw("Speech output off")
    assert len(agent.received) == 2


def test_playback_unavailable_also_degrades():
    def failing_speak(text):
        raise audio.PlaybackUnavailable("no player")

    io = make_io(speak=failing_speak)
    console = FakeConsole(inputs=["", "q"])

    assert session.run_voice_session(FakeAgent(), console, io=io) == 0
    assert console.saw("Speech output off")


def test_silent_turn_does_not_reach_the_agent():
    io = make_io(samples=np.zeros(0, dtype=np.int16))
    agent = FakeAgent()
    console = FakeConsole(inputs=["", "q"])

    session.run_voice_session(agent, console, io=io)
    assert agent.received == []


def test_eof_exits_cleanly():
    io = make_io()
    assert session.run_voice_session(FakeAgent(), FakeConsole(inputs=[]), io=io) == 0


def test_queued_nudges_make_the_twin_speak_first(monkeypatch):
    """Being proactive is the persona's whole point -- it opens the session."""
    monkeypatch.setattr(
        session.store, "pending_nudges", lambda: [{"id": 1, "message": "guitar is stale"}]
    )
    spoken = []
    io = make_io(speak=spoken.append)
    agent = FakeAgent(reply="you haven't touched the guitar project in 23 days")
    console = FakeConsole(inputs=["q"])

    session.run_voice_session(agent, console, io=io)

    assert len(agent.received) == 1
    assert "queued nudges" in agent.received[0]
    # The opening is spoken, not just printed.
    assert spoken == ["you haven't touched the guitar project in 23 days"]


def test_no_nudges_means_no_unprompted_opening():
    agent = FakeAgent()
    session.run_voice_session(agent, FakeConsole(inputs=["q"]), io=make_io())
    assert agent.received == []
