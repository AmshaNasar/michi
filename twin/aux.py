"""Auxiliary model calls.

Spec section 10: cheap, high-volume work that doesn't need the full agent --
scoring how well a search result matches the user's interests, classifying an
email as deadline-bearing -- routes through OpenRouter, keeping the expensive
model reserved for the parts of the system that actually reason.

Falls back to a small Anthropic model when OpenRouter isn't configured, so
nothing here is a hard dependency.
"""

import json
import re
from typing import Any, List, Optional

import httpx

from twin.config import SETTINGS

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Used only when OPENROUTER_API_KEY is absent. Still cheap -- the point is to
# keep aux work off the main agent model, not to require a second provider.
ANTHROPIC_FALLBACK_MODEL = "claude-haiku-4-5-20251001"

TIMEOUT_SECONDS = 60.0


class AuxError(RuntimeError):
    """An auxiliary call failed. Callers should degrade, not crash."""


def _via_openrouter(system: str, user: str, max_tokens: int) -> str:
    response = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": "Bearer {0}".format(SETTINGS.openrouter_api_key),
            "Content-Type": "application/json",
        },
        json={
            "model": SETTINGS.aux_model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AuxError("Unexpected OpenRouter response shape: {0}".format(payload)) from exc


def _via_anthropic(system: str, user: str, max_tokens: int) -> str:
    import anthropic

    SETTINGS.require_agent_key()
    client_kwargs = {"api_key": SETTINGS.anthropic_api_key}
    if SETTINGS.agent_base_url:
        client_kwargs["base_url"] = SETTINGS.agent_base_url
    client = anthropic.Anthropic(**client_kwargs)
    message = client.messages.create(
        model=SETTINGS.aux_model if SETTINGS.agent_base_url else ANTHROPIC_FALLBACK_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in message.content if block.type == "text")


def complete(system: str, user: str, max_tokens: int = 1500) -> str:
    """Run a cheap completion, preferring OpenRouter."""
    try:
        if SETTINGS.aux_enabled:
            return _via_openrouter(system, user, max_tokens)
        return _via_anthropic(system, user, max_tokens)
    except AuxError:
        raise
    except Exception as exc:
        raise AuxError("Auxiliary model call failed: {0}".format(exc)) from exc


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model response.

    Small models wrap JSON in prose or markdown fences often enough that
    json.loads on the raw string is not a reasonable contract.
    """
    if not text:
        raise AuxError("Empty response.")

    stripped = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", stripped, re.S)
    if fenced:
        stripped = fenced.group(1).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost bracketed span.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise AuxError("No JSON found in response: {0}".format(text[:200]))


def complete_json(system: str, user: str, max_tokens: int = 1500) -> Any:
    """Run a cheap completion and parse its response as JSON."""
    return extract_json(complete(system, user, max_tokens))
