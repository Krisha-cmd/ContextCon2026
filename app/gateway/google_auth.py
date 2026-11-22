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

_lock = threading.Lock()
_creds: Credentials | None = None


def access_token(settings: Settings) -> str:
    """A valid bearer token, refreshed when expired. Thread-safe; blocks briefly on refresh."""
    global _creds
    with _lock:
        if _creds is None:
            _creds, _ = google.auth.default(scopes=list(settings.actions.google_scopes))
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token


def reset() -> None:
    global _creds
    with _lock:
        _creds = None
