"""Environment-level settings.

Runtime settings (API keys, model names, database URL) live here and come from
the environment. Everything that describes *the user* — priorities, interests,
staleness threshold, connected apps — lives in the `user_profile` table
instead, because the agent needs to read and update it as it learns.
"""

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Read-scoped and write-scoped Google permissions are requested separately so
# that what the agent can do unattended stays auditable (spec section 11).
GMAIL_READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]
GMAIL_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
]
CALENDAR_READ_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
]
CALENDAR_WRITE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]

# Only requested when a Pub/Sub topic is configured, so users who don't want
# push notifications aren't asked to grant a scope they'll never use.
PUBSUB_SCOPES = [
    "https://www.googleapis.com/auth/pubsub",
]


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if value else default


@dataclass
class Settings:
    anthropic_api_key: str
    agent_model: str
    # Overrides where the agent loop sends requests. Set this to OpenRouter's
    # Anthropic-compatible endpoint to run the whole system on OpenRouter
    # credits; the wire format and tool-use blocks are identical, so nothing
    # in the agent loop changes.
    #
    # Deliberately a TWIN_-prefixed variable rather than relying on the SDK
    # picking up ANTHROPIC_BASE_URL: python-dotenv does not override variables
    # already exported in the shell, so an existing ANTHROPIC_BASE_URL would
    # silently win over the .env value and be very hard to diagnose.
    agent_base_url: Optional[str]
    openrouter_api_key: Optional[str]
    aux_model: str
    database_url: str
    openai_api_key: Optional[str]
    embedding_model: str
    exa_api_key: Optional[str]
    eventbrite_api_key: Optional[str]
    google_client_secrets_file: str
    # Full resource names, e.g. projects/my-proj/topics/gmail-push and
    # projects/my-proj/subscriptions/gmail-pull. Both empty disables push.
    gmail_pubsub_topic: Optional[str]
    gmail_pubsub_subscription: Optional[str]
    stt_model: str
    tts_model: str
    tts_voice: str

    @property
    def embeddings_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def voice_enabled(self) -> bool:
        """Voice rides on the same OpenAI key as embeddings."""
        return bool(self.openai_api_key)

    @property
    def gmail_push_enabled(self) -> bool:
        """Push needs both a topic to watch into and a subscription to pull from."""
        return bool(self.gmail_pubsub_topic and self.gmail_pubsub_subscription)

    @property
    def aux_enabled(self) -> bool:
        """Whether cheap background calls can be routed away from the main model."""
        return bool(self.openrouter_api_key)

    def require_agent_key(self) -> None:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
            )


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        agent_model=_env("TWIN_AGENT_MODEL", "claude-opus-5"),
        agent_base_url=_env("TWIN_AGENT_BASE_URL") or None,
        openrouter_api_key=_env("OPENROUTER_API_KEY") or None,
        aux_model=_env("TWIN_AUX_MODEL", "anthropic/claude-haiku-4.5"),
        database_url=_env(
            "TWIN_DATABASE_URL",
            "postgresql://{0}@localhost:5432/digital_twin".format(
                os.environ.get("USER", "postgres")
            ),
        ),
        openai_api_key=_env("OPENAI_API_KEY") or None,
        embedding_model=_env("TWIN_EMBEDDING_MODEL", "text-embedding-3-small"),
        exa_api_key=_env("EXA_API_KEY") or None,
        eventbrite_api_key=_env("EVENTBRITE_API_KEY") or None,
        google_client_secrets_file=_env(
            "GOOGLE_CLIENT_SECRETS_FILE", "./google_client_secret.json"
        ),
        gmail_pubsub_topic=_env("GMAIL_PUBSUB_TOPIC") or None,
        gmail_pubsub_subscription=_env("GMAIL_PUBSUB_SUBSCRIPTION") or None,
        stt_model=_env("TWIN_STT_MODEL", "whisper-1"),
        tts_model=_env("TWIN_TTS_MODEL", "gpt-4o-mini-tts"),
        tts_voice=_env("TWIN_TTS_VOICE", "alloy"),
    )


SETTINGS = load_settings()
