"""Offline tests for the write path: delta shapes, date round-trip, and the
commit client's dry-run/live behavior. No network or credentials needed.

Run with:  python3 -m pytest tests/  (or)  python3 tests/test_commit.py
"""

import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thingsgnome.model import Database, Status, _date_to_ts, _ts_to_date
from thingsgnome.sync.auth import Account
from thingsgnome.sync.client import ThingsReadClient
from thingsgnome.sync import protocol as P
from thingsgnome.sync.commit import CommitError, ThingsWriteClient

from tests.test_replay import build_synthetic_state


FAKE_ACCOUNT = Account(
    email="test@example.com",
    history_key="00000000-0000-0000-0000-000000000000",
    maildrop_email=None,
    head_index=0,
    session_secret=None,
)


def _open_task(db):
    return next(t for t in db.tasks if t.is_open)


def test_complete_uncomplete_cancel_delta_shapes():
    db = Database.from_entities(build_synthetic_state())
    t = _open_task(db)

    complete = t.complete_delta()
    assert set(complete.keys()) == {"ss", "sp", "md"}
    assert complete["ss"] == P.STATUS_COMPLETE
    assert isinstance(complete["sp"], float) and complete["sp"] > 0
    assert isinstance(complete["md"], float) and complete["md"] > 0

    uncomplete = t.uncomplete_delta()
    assert uncomplete == {"ss": P.STATUS_TODO, "sp": None, "md": uncomplete["md"]}
    # sp must be an EXPLICIT None key, not simply absent.
    assert "sp" in uncomplete and uncomplete["sp"] is None

    cancel = t.cancel_delta()
    assert set(cancel.keys()) == {"ss", "sp", "md"}
    assert cancel["ss"] == P.STATUS_CANCELLED

    print("OK: complete/uncomplete/cancel delta shapes")


def test_date_round_trip():
    for d in (date.today(), date(2026, 1, 1), date(2000, 2, 29), date.today() + timedelta(days=400)):
        ts = _date_to_ts(d)
        assert _ts_to_date(ts) == d, (d, ts, _ts_to_date(ts))
    assert _date_to_ts(None) is None
    assert _ts_to_date(None) is None
    print("OK: date <-> timestamp round-trip")


def test_dry_run_never_touches_network():
    client = ThingsWriteClient(FAKE_ACCOUNT)

    def _boom(*a, **k):
        raise AssertionError("dry-run must not make a network request")

    client._session.post = _boom

    result = client.edit("UUID000000000000000000", {"ss": 3, "sp": 1.0, "md": 1.0},
                          ancestor_index=42, dry_run=True)
    assert result.dry_run is True
    assert result.server_head_index == 42
    assert result.body == {
        "UUID000000000000000000": {
            "t": P.UPDATE_EDIT, "e": P.ENTITY_TASK,
            "p": {"ss": 3, "sp": 1.0, "md": 1.0},
        }
    }
    print("OK: dry-run never touches the network")


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json


def test_live_commit_sends_expected_request_and_parses_head():
    client = ThingsWriteClient(FAKE_ACCOUNT)
    captured = {}

    def _fake_post(url, params=None, json=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return _FakeResponse(200, {"server-head-index": 101})

    client._session.post = _fake_post

    result = client.edit("UUID000000000000000000", {"ss": 0, "sp": None, "md": 5.0},
                          ancestor_index=100, dry_run=False)

    assert result.dry_run is False
    assert result.server_head_index == 101
    assert captured["params"] == {"ancestor-index": "100", "_cnt": "1"}
    assert captured["url"].endswith("/commit")
    assert captured["json"] == {
        "UUID000000000000000000": {"t": P.UPDATE_EDIT, "e": P.ENTITY_TASK,
                                    "p": {"ss": 0, "sp": None, "md": 5.0}}
    }
    print("OK: live commit sends ancestor-index/_cnt and parses server-head-index")


def test_live_commit_raises_on_error_response():
    client = ThingsWriteClient(FAKE_ACCOUNT)
    client._session.post = lambda *a, **k: _FakeResponse(401, {}, text="expired")
    try:
        client.edit("UUID000000000000000000", {"ss": 3, "sp": 1.0, "md": 1.0},
                     ancestor_index=1, dry_run=False)
        raise AssertionError("expected CommitError")
    except CommitError as e:
        assert "expired" in str(e).lower() or "session" in str(e).lower()
    print("OK: non-2xx commit response raises CommitError")


def test_delta_replays_through_existing_pipeline():
    """The exact thing window.py does after a successful commit: apply the delta
    locally via the same _apply() the read client uses, then rebuild Database."""
    entities = build_synthetic_state()
    db = Database.from_entities(entities)
    t = _open_task(db)
    delta = t.complete_delta()

    ThingsReadClient._apply(entities, t.uuid, {"t": P.UPDATE_EDIT, "e": P.ENTITY_TASK, "p": delta})

    db2 = Database.from_entities(entities)
    updated = next(x for x in db2.tasks if x.uuid == t.uuid)
    assert updated.status is Status.COMPLETED
    assert updated.completion_date == date.today()
    lists = db2.categorize()
    assert any(x.uuid == t.uuid for x in lists.logbook)

    # and back
    delta2 = updated.uncomplete_delta()
    ThingsReadClient._apply(entities, t.uuid, {"t": P.UPDATE_EDIT, "e": P.ENTITY_TASK, "p": delta2})
    db3 = Database.from_entities(entities)
    reopened = next(x for x in db3.tasks if x.uuid == t.uuid)
    assert reopened.status is Status.OPEN
    assert reopened.completion_date is None
    print("OK: complete/uncomplete deltas round-trip through the real replay pipeline")


if __name__ == "__main__":
    test_complete_uncomplete_cancel_delta_shapes()
    test_date_round_trip()
    test_dry_run_never_touches_network()
    test_live_commit_sends_expected_request_and_parses_head()
    test_live_commit_raises_on_error_response()
    test_delta_replays_through_existing_pipeline()
    print("\nAll commit tests passed.")
