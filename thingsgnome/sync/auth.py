"""Things Cloud authentication.

Two steps (see protocol.py for the full picture):
  1. resolve the account  -> history_key
  2. open a shared session -> head_index (where the log currently ends)

Uses `requests` (ubiquitous on Fedora via python3-requests). Network errors and
bad credentials are surfaced as AuthError so the UI can show a clean message.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import quote

import requests

from .protocol import ACCOUNT_URL, SESSION_URL, DEFAULT_USER_AGENT


class AuthError(Exception):
    """Raised when login fails (bad credentials, network, or unexpected response)."""


@dataclass
class Account:
    email: str
    history_key: str
    maildrop_email: str | None
    head_index: int
    session_secret: str | None

    @property
    def base_url(self) -> str:
        from .protocol import API_BASE

        return f"{API_BASE}/history/{self.history_key}"


def _account_step(email: str, password: str, user_agent: str, timeout: float) -> dict:
    url = ACCOUNT_URL.format(email=quote(email, safe=""))
    headers = {
        "Authorization": f"Password {quote(password, safe="'")}",
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise AuthError(f"Could not reach Things Cloud: {e}") from e
    if r.status_code == 401:
        raise AuthError("Incorrect email or password.")
    if not r.ok:
        raise AuthError(f"Account lookup failed (HTTP {r.status_code}).")
    try:
        return r.json()
    except ValueError as e:
        raise AuthError("Account response was not valid JSON.") from e


def _session_step(email: str, password: str, user_agent: str, timeout: float) -> dict:
    payload = json.dumps({"ep": {"e": email, "p": password}}).encode("utf-8")
    token = base64.b64encode(payload).decode("utf-8")
    headers = {
        "Authorization": f"B64SON {token}",
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    try:
        r = requests.post(SESSION_URL, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise AuthError(f"Could not open a session: {e}") from e
    if not r.ok:
        raise AuthError(f"Session request failed (HTTP {r.status_code}).")
    try:
        return r.json()
    except ValueError as e:
        raise AuthError("Session response was not valid JSON.") from e


def login(
    email: str,
    password: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 30.0,
) -> Account:
    """Authenticate and return an Account handle (does not fetch any tasks yet)."""
    info = _account_step(email, password, user_agent, timeout)
    history_key = info.get("history-key")
    if not history_key:
        raise AuthError("Login succeeded but no history-key was returned.")

    head_index = 0
    secret = None
    try:
        session = _session_step(email, password, user_agent, timeout)
        head_index = int(session.get("headIndex", 0))
        secret = session.get("historyKeySessionSecret")
    except AuthError:
        # The session step is only needed for writing. Reading works with the
        # history-key alone, so we tolerate a failure here for the read-only MVP.
        head_index = 0

    return Account(
        email=email,
        history_key=history_key,
        maildrop_email=info.get("maildrop-email"),
        head_index=head_index,
        session_secret=secret,
    )
