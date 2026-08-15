"""Things Cloud read client: fetch the history log and replay it to current state.

The server stores your data as an append-only event log. This client:
  * pages through `/items` from a start index until the log is exhausted,
  * replays NEW (full) and EDIT (delta) events into a per-uuid state map, and
  * returns a `ReplayResult` (raw, decoded entities + the new head offset).

It is deliberately READ-ONLY. Turning the decoded entities into areas / projects /
tasks lives in `model.py`, so this layer stays a faithful mirror of the wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .auth import Account
from .protocol import (
    UPDATE_NEW,
    UPDATE_EDIT,
    UPDATE_BASELINE_OR_DELETE,
    decode_fields,
    sync_headers,
)


class SyncError(Exception):
    pass


@dataclass
class Entity:
    """One replayed item, kept in human-readable form."""

    uuid: str
    entity_type: str           # e.g. "Task6", "ChecklistItem3", "Area..."
    fields: dict = field(default_factory=dict)


@dataclass
class ReplayResult:
    entities: dict[str, Entity]
    head_index: int
    new_count: int             # how many log items were processed this run


class ThingsReadClient:
    def __init__(
        self,
        account: Account,
        *,
        user_agent: str | None = None,
        timeout: float = 60.0,
        max_pages: int = 2000,
    ) -> None:
        self._account = account
        self._timeout = timeout
        self._max_pages = max_pages
        self._session = requests.Session()
        headers = sync_headers(user_agent) if user_agent else sync_headers()
        self._session.headers.update(headers)

    # -- low level ----------------------------------------------------------------
    def _fetch_page(self, start_index: int) -> dict:
        url = f"{self._account.base_url}/items"
        try:
            r = self._session.get(
                url, params={"start-index": str(start_index)}, timeout=self._timeout
            )
        except requests.RequestException as e:
            raise SyncError(f"Network error while syncing: {e}") from e
        if r.status_code == 401:
            raise SyncError("Session expired - please sign in again.")
        if not r.ok:
            raise SyncError(f"Sync failed (HTTP {r.status_code}).")
        try:
            return r.json()
        except ValueError as e:
            raise SyncError("Sync response was not valid JSON.") from e

    # -- replay -------------------------------------------------------------------
    @staticmethod
    def _apply(entities: dict[str, Entity], uuid: str, body: dict) -> None:
        kind = body.get("t")
        entity_type = body.get("e", "")
        payload = body.get("p", {}) or {}
        decoded = decode_fields(payload)

        def create_or_merge() -> None:
            ent = entities.get(uuid)
            if ent is None:
                entities[uuid] = Entity(uuid=uuid, entity_type=entity_type, fields=decoded)
            else:
                ent.fields.update(decoded)
                if entity_type:
                    ent.entity_type = entity_type

        if kind == UPDATE_NEW:
            entities[uuid] = Entity(uuid=uuid, entity_type=entity_type, fields=decoded)
        elif kind == UPDATE_EDIT:
            # An edit for something we never saw created: keep what it carries so we
            # don't lose data if the NEW was compacted out of this slice of the log.
            create_or_merge()
        elif kind == UPDATE_BASELINE_OR_DELETE:
            # t == 2: carries a state baseline when it has a payload -- apply it
            # like a create-or-merge. When empty, treat it as a deletion.
            #
            # This used to be a no-op: an earlier guess ("empty t=2 = delete")
            # was reverted because it risked dropping live data with no
            # confirmation it was even right. It's since been confirmed against
            # a real account: every t=2 event observed there (256 of them, zero
            # exceptions) had an empty payload, and cross-referencing specific
            # uuids showed real, titled, non-trashed tasks the user had deleted
            # on another device still showing as open here -- exactly what a
            # dropped delete signal looks like. Synthesizing `trashed=True`
            # (rather than discarding the entity) reuses every existing
            # trashed-based filter instead of adding a new state, and an
            # explicit `tr: false` EDIT arriving later still un-trashes it
            # correctly since EDITs always take precedence via create_or_merge.
            if decoded:
                create_or_merge()
            else:
                ent = entities.get(uuid)
                if ent is not None:
                    ent.fields["trashed"] = True
        # any other kind is ignored on purpose

    def replay(
        self,
        *,
        start_index: int = 0,
        entities: dict[str, Entity] | None = None,
    ) -> ReplayResult:
        """Replay the log from `start_index`, optionally continuing prior `entities`.

        Pass the previous ReplayResult.entities and its head_index to do an
        incremental sync instead of re-reading everything.
        """
        state: dict[str, Entity] = entities if entities is not None else {}
        index = start_index
        processed = 0
        head = start_index

        # The log is paged: `start-index` is an OFFSET into the log and the server
        # returns a batch of items from there. `current-item-index` is the log's
        # HEAD (total length) and stays ~constant across pages -- it is NOT the next
        # offset. So we advance the offset by the number of items the page actually
        # returned and keep going until a page comes back empty. (Advancing straight
        # to `current-item-index` would consume only the first batch and leave you
        # with a stale snapshot.)
        for _ in range(self._max_pages):
            page = self._fetch_page(index)
            items = page.get("items") or []

            server_head = page.get("current-item-index")
            if isinstance(server_head, int):
                head = max(head, server_head)

            if not items:
                break

            for item in items:
                # each item is a single-key dict {uuid: body}
                if not isinstance(item, dict) or not item:
                    continue
                uuid, body = next(iter(item.items()))
                if isinstance(body, dict):
                    self._apply(state, uuid, body)
                    processed += 1

            # advance by what the server returned (offset into the log), not by the
            # count we could parse, so a skipped malformed item never desyncs us.
            index += len(items)

            # if the server tells us a head and we've reached it, we're done.
            if head and index >= head:
                break

        head_index = max(index, head)
        return ReplayResult(entities=state, head_index=head_index, new_count=processed)
