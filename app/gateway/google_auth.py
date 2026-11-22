"""OAuth access tokens for Google Workspace APIs (Calendar, Gmail) from Application Default
Credentials. Set up once with:

  gcloud auth application-default login --scopes=openid,<the scopes in config actions.google_scopes>

Lives in app/gateway because refreshing a token is an outside HTTP call.
"""

from __future__ import annotations

import threading

import google.auth
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from app.config import Settings

CLOUD_PLATFORM = ("https://www.googleapis.com/auth/cloud-platform",)

_lock = threading.Lock()
_creds: dict[tuple[str, ...], Credentials] = {}


def scopes_for(group: str, settings: Settings) -> tuple[str, ...]:
    """Workspace APIs (gcal, gmail) use the configured user scopes; GCP APIs cloud-platform."""
    return CLOUD_PLATFORM if group == "cloudrun" else tuple(settings.actions.google_scopes)


def access_token(settings: Settings, group: str = "gcal") -> str:
    """A valid bearer token for the group, refreshed when expired. Thread-safe."""
    scopes = scopes_for(group, settings)
    with _lock:
        creds = _creds.get(scopes)
        if creds is None:
            creds, _ = google.auth.default(scopes=list(scopes))
            _creds[scopes] = creds
        if not creds.valid:
            creds.refresh(Request())
        return creds.token


def reset() -> None:
    with _lock:
        _creds.clear()
