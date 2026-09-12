"""The voice REPL.

Wires microphone -> transcription -> the existing agent -> speech. The agent
itself is untouched: this layer only converts between audio and the same
strings `twin.cli chat` already passes around (spec section 9).

The three I/O calls are injected so the loop's wiring can be tested without
touching real audio hardware or spending API calls.
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np

from twin.memory import store
from twin.voice import audio, stt, tts

# Spoken words that end the session, after transcription.
EXIT_PHRASES = {"exit", "quit", "goodbye", "bye", "stop", "that's all", "thats all"}


@dataclass
class VoiceIO:
    record: Callable[[], np.ndarray]
    transcribe: Callable[[bytes], str]
    speak: Callable[[str], None]
    is_silence: Callable[[np.ndarray], bool]


def default_io(stop_signal: Optional[Callable[[], None]] = None) -> VoiceIO:
    signal = stop_signal or audio.wait_for_enter
    return VoiceIO(
        record=lambda: audio.record_until(signal),
        transcribe=stt.transcribe,
        speak=tts.speak,
        is_silence=audio.is_silence,
    )


def normalize_utterance(text: str) -> str:
    """Lowercase and strip punctuation for exit-phrase matching."""
    return text.strip().strip(".!?,").lower()


def is_exit_phrase(text: str) -> bool:
    return normalize_utterance(text) in EXIT_PHRASES


def capture_utterance(io: VoiceIO, console: Any) -> Optional[str]:
    """Record and transcribe one utterance, or None if nothing was said."""
    samples = io.record()

    if io.is_silence(samples):
        console.print("  [dim]didn't catch anything -- try again.[/]")
        return None

    wav = audio.to_wav_bytes(samples)
    with console.status("[dim]transcribing...[/]"):
        try:
            text = io.transcribe(wav)
        except stt.TranscriptionError as exc:
            console.print("  [red]{0}[/]".format(exc))
            return None

    # Strip here rather than trusting the transcriber: VoiceIO.transcribe is a
    # pluggable interface, and a local model may not normalize its output.
    text = (text or "").strip()
    if not text:
        console.print("  [dim]didn't catch anything -- try again.[/]")
        return None
    return text


def run_voice_session(
    agent: Any,
    console: Any,
    io: Optional[VoiceIO] = None,
    max_turns: Optional[int] = None,
) -> int:
    """Run the voice loop until the user exits. Returns a process exit code."""
    io = io or default_io()

    # If speech synthesis breaks mid-session, fall back to text rather than
    # dropping the user out of the conversation entirely.
    speech_ok = True

    def respond(text: str) -> None:
        nonlocal speech_ok
        console.print("[bold cyan]twin[/] " + text)
        if not speech_ok:
            return
        try:
            io.speak(text)
        except (tts.SynthesisError, audio.PlaybackUnavailable) as exc:
            console.print("  [yellow]Speech output off for this session: {0}[/]".format(exc))
            speech_ok = False

    pending = store.pending_nudges()
    if pending:
        with console.status("[dim]thinking...[/]"):
            opening = agent.send(
                "(System: the user just opened a voice session. You have queued "
                "nudges. Open the conversation yourself with the most important "
                "one, in your own voice, citing the real data behind it. Keep it "
                "to a couple of sentences -- this will be read aloud.)"
            )
        respond(opening)

    turns = 0
    while max_turns is None or turns < max_turns:
        turns += 1
        console.print()
        try:
            typed = console.input(
                "[bold green]you[/] [dim](Enter to talk, or type a message; 'q' quits)[/] "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/]")
            return 0

        if typed.lower() in {"q", "exit", "quit"}:
            console.print("[dim]bye.[/]")
            return 0

        if typed:
            # Typing still works mid-voice-session; useful when dictation is
            # awkward or the room is loud.
            message = typed
        else:
            console.print("  [dim]listening... press Enter when done.[/]")
            message = capture_utterance(io, console)
            if message is None:
                continue
            console.print("[bold green]you[/] {0}".format(message))

            if is_exit_phrase(message):
                console.print("[dim]bye.[/]")
                return 0

        console.print()
        with console.status("[dim]thinking...[/]"):
            reply = agent.send(message)
        respond(reply)

    return 0
