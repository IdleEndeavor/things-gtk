"""Things Cloud write client: commit EDIT deltas onto the history log.

Mirrors ThingsReadClient's shape but for the `commit` endpoint. Confirmed against
two independent community implementations (disrupted/things-cloud-api,
evanpurkhiser/things3-cloud) that both exercise this endpoint against live
accounts: `POST .../commit?ancestor-index={head}&_cnt=1` with body
`{uuid: {"t":0|1, "e":"Task6", "p":{...}}}`, returning `{"server-head-index": N}`.

Things Cloud has no optimistic merge -- a stale `ancestor_index` risks the
server rejecting the write or racing a real device's concurrent edit. Callers
MUST read the head fresh (a `ThingsReadClient.replay()`) immediately before
calling `edit()`; this module does not do that for you, since it has no
opinion on your local cache.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .auth import Account
from .protocol import ENTITY_TASK, UPDATE_EDIT, sync_headers


class CommitError(Exception):
    pass


@dataclass
class CommitResult:
    server_head_index: int
    dry_run: bool
    body: dict  # the exact wire body that was (or, in dry-run, would be) sent


class ThingsWriteClient:
    def __init__(
        self,
        account: Account,
        *,
        user_agent: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._account = account
        self._timeout = timeout
        self._session = requests.Session()
        headers = sync_headers(user_agent) if user_agent else sync_headers()
        self._session.headers.update(headers)

    def edit(
        self,
        uuid: str,
        delta: dict,
        *,
        ancestor_index: int,
        entity_type: str = ENTITY_TASK,
        dry_run: bool = True,
    ) -> CommitResult:
        """Commit a partial EDIT. `delta` is a two-letter-keyed dict; pass explicit
        None for fields that should be cleared -- this method sends exactly what
        you give it, with no filtering."""
        body = {uuid: {"t": UPDATE_EDIT, "e": entity_type, "p": delta}}
        return self._commit(body, ancestor_index=ancestor_index, dry_run=dry_run)

    # No `new()` (t:0 NEW-object commit) yet: a NEW payload must be the full
    # ~34-field object real clients send (see REVERSE-ENGINEERING.md §6), and
    # that field set has an unresolved disagreement between reference
    # implementations (e.g. the `nt` default). Add it once §8 item 7 resolves
    # that, backed by a payload-builder in model.py and a test, the same way
    # `edit()` is backed by Task.complete_delta() and tests/test_commit.py.

    def _commit(self, body: dict, *, ancestor_index: int, dry_run: bool) -> CommitResult:
        if dry_run:
            # Never touch the network in dry-run -- this is the safety default a
            # caller can trust without reading the rest of this method.
            return CommitResult(server_head_index=ancestor_index, dry_run=True, body=body)

        url = f"{self._account.base_url}/commit"
        try:
            r = self._session.post(
                url,
                params={"ancestor-index": str(ancestor_index), "_cnt": "1"},
                json=body,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            raise CommitError(f"Network error while committing: {e}") from e
        if r.status_code == 401:
            raise CommitError("Session expired - please sign in again.")
        if not r.ok:
            raise CommitError(f"Commit failed (HTTP {r.status_code}): {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as e:
            raise CommitError("Commit response was not valid JSON.") from e
        head = data.get("server-head-index")
        if not isinstance(head, int):
            raise CommitError(f"Commit response missing server-head-index: {data}")
        return CommitResult(server_head_index=head, dry_run=False, body=body)
