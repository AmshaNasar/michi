"""OAuth for the Google integrations (Gmail, Calendar).

Tokens live in the OS keychain, never on disk. The local-server flow is used
because v1 runs on the user's own machine with no public endpoint.
"""

import os
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from twin import credentials as credstore
from twin.config import (
    CALENDAR_READ_SCOPES,
    CALENDAR_WRITE_SCOPES,
    GMAIL_READ_SCOPES,
    GMAIL_WRITE_SCOPES,
    PUBSUB_SCOPES,
    SETTINGS,
)

# Read and write scopes are declared separately (spec section 11) so a
# read-only deployment can request strictly less. Both are requested here
# because v1 drafts replies, but the split stays visible and auditable.
SERVICE_SCOPES = {
    "gmail": GMAIL_READ_SCOPES + GMAIL_WRITE_SCOPES,
    "calendar": CALENDAR_READ_SCOPES + CALENDAR_WRITE_SCOPES,
}

# Pub/Sub is addressed through the same OAuth credentials as Gmail rather than
# a separate service account: this is a single-user local app, and one consent
# screen is less to go wrong than a second credential file.
_API_VERSIONS = {
    "gmail": ("gmail", "v1"),
    "calendar": ("calendar", "v3"),
    "pubsub": ("pubsub", "v1"),
}


class NotConnected(RuntimeError):
    """Raised when a tool is called for a service the user hasn't connected."""


def _scopes_for(service: str) -> List[str]:
    if service not in SERVICE_SCOPES:
        raise ValueError("Unknown Google service: {0}".format(service))
    scopes = list(SERVICE_SCOPES[service])
    if service == "gmail" and SETTINGS.gmail_push_enabled:
        # Requested only when push is configured. Adding a scope invalidates
        # the stored token, so users not using push are never forced to
        # re-authorize for a capability they don't want.
        scopes = scopes + PUBSUB_SCOPES
    return scopes


# Pub/Sub piggybacks on the Gmail credential rather than having its own.
_CREDENTIAL_OWNER = {"pubsub": "gmail"}


def load_credentials(service: str) -> Optional[Credentials]:
    """Return stored credentials, refreshing them if they've expired."""
    payload = credstore.load_token(service)
    if not payload:
        return None

    creds = Credentials.from_authorized_user_info(payload, _scopes_for(service))
    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        credstore.save_token(service, _credentials_to_dict(creds))
        return creds

    return None


def _credentials_to_dict(creds: Credentials) -> dict:
    import json

    return json.loads(creds.to_json())


def connect(service: str) -> Credentials:
    """Run the interactive consent flow and store the resulting token.

    Called from onboarding, where the user has explicitly opted in -- never
    triggered implicitly by a tool call.
    """
    secrets_file = SETTINGS.google_client_secrets_file
    if not os.path.exists(secrets_file):
        raise FileNotFoundError(
            "Google OAuth client secrets not found at '{0}'.\n"
            "Create an OAuth 2.0 Client ID of type 'Desktop app' in Google Cloud "
            "Console, download the JSON, and point GOOGLE_CLIENT_SECRETS_FILE at "
            "it in your .env.".format(secrets_file)
        )

    flow = InstalledAppFlow.from_client_secrets_file(secrets_file, _scopes_for(service))
    creds = flow.run_local_server(port=0, prompt="consent")
    credstore.save_token(service, _credentials_to_dict(creds))
    return creds


def client(service: str):
    """Build an authorized Google API client, or raise NotConnected."""
    owner = _CREDENTIAL_OWNER.get(service, service)
    creds = load_credentials(owner)
    if creds is None:
        raise NotConnected(
            "{0} is not connected. Run onboarding (`python -m twin.cli onboard`) "
            "and enable it.".format(owner)
        )
    api, version = _API_VERSIONS[service]
    return build(api, version, credentials=creds, cache_discovery=False)
