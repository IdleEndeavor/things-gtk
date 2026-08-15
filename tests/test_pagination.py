"""Regression test for the history pagination / replay loop.

Reproduces the live bug where only the first server batch was consumed:
  * a title set by an EDIT in a LATER batch must still be applied, and
  * items in later batches (the most recent events) must not go missing.

This drives ThingsReadClient.replay() through a fake, batched _fetch_page so we
exercise the real pagination logic without a network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thingsgnome.sync.client import ThingsReadClient
from thingsgnome.model import Database


def _build_fake_log():
    """A log of NEW + EDIT events, deliberately spanning several batches."""
    log = []
    # 0: create an area
    log.append({"AREA000000000000000000": {"t": 0, "e": "Area3", "p": {"tt": "Goals", "ix": 0}}})
    # 1: create a project with NO title yet (as Things often does on "+")
    log.append({"PROJ000000000000000000": {"t": 0, "e": "Task6",
                "p": {"tp": 1, "ss": 0, "ar": ["AREA000000000000000000"], "ix": 0}}})
    # 2..N: filler tasks so the title-setting EDIT lands in a later batch
    for i in range(600):
        log.append({f"FILL{i:018d}": {"t": 0, "e": "Task6",
                    "p": {"tt": f"Filler {i}", "tp": 0, "ss": 0, "ix": i}}})
    # later: EDIT that finally sets the project's title (this is past batch 1)
    log.append({"PROJ000000000000000000": {"t": 1, "e": "Task6", "p": {"tt": "Yearly Goals"}}})
    # the most-recent task, right at the tail of the log
    log.append({"TASKLATEST0000000000000": {"t": 0, "e": "Task6",
                "p": {"tt": "Renew insurance", "tp": 0, "ss": 0,
                      "ar": ["AREA000000000000000000"], "ix": 999}}})
    return log


class FakeClient(ThingsReadClient):
    """ThingsReadClient with _fetch_page replaced by an in-memory batched log."""

    BATCH = 500

    def __init__(self, log):
        self._log = log
        self._max_pages = 2000
        # NB: skip the real __init__ (no account / requests session needed)

    def _fetch_page(self, start_index: int) -> dict:
        batch = self._log[start_index:start_index + self.BATCH]
        return {"current-item-index": len(self._log), "items": batch}


def test_full_log_is_replayed():
    log = _build_fake_log()
    client = FakeClient(log)
    result = client.replay()

    # every event consumed, head at the true end of the log
    assert result.new_count == len(log), (result.new_count, len(log))
    assert result.head_index == len(log)

    db = Database.from_entities(result.entities)

    # the EDIT from a later batch was applied -> project is titled, not "Untitled"
    proj = db.project("PROJ000000000000000000")
    assert proj is not None and proj.title == "Yearly Goals", proj and proj.title

    # the most-recent task at the tail of the log is present (was missing before)
    titles = {t.title for t in db.tasks}
    assert "Renew insurance" in titles, "tail-of-log item went missing"

    print("OK: full log replayed across batches")
    print(f"OK: {result.new_count} events, head={result.head_index}")
    print("OK: later-batch EDIT applied (project titled 'Yearly Goals')")
    print("OK: most-recent item present")


def test_t2_and_fragments():
    """t=2-with-payload is a baseline; empty t=2 is a deletion; trashed hides; empty fragments skip."""
    import thingsgnome.sync.protocol as P  # noqa: F401
    from thingsgnome.sync.client import Entity  # noqa: F401

    entities: dict[str, Entity] = {}
    A = ThingsReadClient._apply

    # t=2 WITH payload acts as a state baseline (entity we never saw NEW'd)
    A(entities, "BASE000000000000000000",
      {"t": 2, "e": "Task6", "p": {"tt": "Renew tax", "tp": 0, "ss": 0, "st": 1}})
    # NEW then EMPTY t=2 -> deletion. Confirmed against a real account: every
    # t=2 event observed there (256, zero exceptions) had an empty payload, and
    # cross-referencing specific uuids showed real, titled, non-trashed tasks
    # the user had deleted on another device still showing as open here. The
    # entity stays in raw state (trashed=True is synthesized), it just
    # disappears from every view -- same treatment as an explicit `tr:true`.
    A(entities, "DELD000000000000000000",
      {"t": 0, "e": "Task6", "p": {"tt": "Should be gone", "tp": 0, "ss": 0, "st": 1}})
    A(entities, "DELD000000000000000000", {"t": 2, "e": "Task6", "p": {}})
    # ...and an explicit un-delete (tr:false) EDIT arriving after still wins,
    # since EDITs always take precedence via create_or_merge.
    A(entities, "UNDEL000000000000000000",
      {"t": 0, "e": "Task6", "p": {"tt": "Restored", "tp": 0, "ss": 0, "st": 1}})
    A(entities, "UNDEL000000000000000000", {"t": 2, "e": "Task6", "p": {}})
    A(entities, "UNDEL000000000000000000", {"t": 1, "e": "Task6", "p": {"tr": False}})
    # a task explicitly trashed -> stays in raw state but must be hidden from views
    A(entities, "TRSH000000000000000000",
      {"t": 0, "e": "Task6", "p": {"tt": "Gone", "tp": 0, "ss": 0, "st": 1, "tr": True}})
    # a titled edit-only fragment -> KEPT (real, just partially known)
    A(entities, "TFRG000000000000000000",
      {"t": 1, "e": "Task6", "p": {"tt": "Has title", "md": 1.0}})
    # a totally empty edit-only fragment -> skipped
    A(entities, "EFRG000000000000000000",
      {"t": 1, "e": "Task6", "p": {"md": 1.0}})
    # untitled, untyped, unattached fragment that only carries status/destination
    # (a stray "mark done" or "move to Anytime" EDIT for an entity whose NEW was
    # never captured) -> skipped. On a real account ~35% of Task6 entities were
    # exactly this shape and used to be materialized as floating "(untitled)"
    # open tasks -- status/destination alone are not enough signal.
    A(entities, "SFRG000000000000000000",
      {"t": 1, "e": "Task6", "p": {"ss": 0, "st": 1, "md": 1.0}})
    # untitled but has an explicit `type` -> KEPT (type presence is real
    # classification evidence, even without a title)
    A(entities, "TYFRG00000000000000000",
      {"t": 1, "e": "Task6", "p": {"tp": 0, "md": 1.0}})
    # untitled but attached to a project -> KEPT (has_parent)
    A(entities, "PARFRG0000000000000000",
      {"t": 1, "e": "Task6", "p": {"pr": ["BASE000000000000000000"], "md": 1.0}})

    assert "DELD000000000000000000" in entities, "empty t=2 marks trashed, does not discard the entity"
    assert entities["DELD000000000000000000"].fields.get("trashed") is True, \
        "empty t=2 must synthesize trashed=True"

    db = Database.from_entities(entities)

    lists = db.categorize()
    visible = {t.title for grp in (lists.inbox, lists.today, lists.upcoming,
                                   lists.anytime, lists.someday, lists.logbook)
               for t in grp}
    assert "Renew tax" in visible, "t=2 baseline task should be visible"
    assert "Should be gone" not in visible, "empty t=2 (deletion) must be hidden from every view"
    assert "Restored" in visible, "a later explicit tr:false EDIT must win over an earlier empty t=2"
    assert "Has title" in visible, "titled edit-only fragment should be kept"
    assert "Gone" not in visible, "trashed task should be hidden from views"

    assert "EFRG000000000000000000" not in {t.uuid for t in db.tasks}, \
        "empty fragment should be skipped"
    # nothing should have silently fallen into Inbox without an explicit st==0
    assert all(t.destination.name == "INBOX" for t in lists.inbox)

    task_uuids = {t.uuid for t in db.tasks}
    assert "SFRG000000000000000000" not in task_uuids, \
        "status/destination-only fragment should be skipped (no title, no type, no parent)"
    assert "TYFRG00000000000000000" in task_uuids, \
        "untitled fragment WITH a type should still be kept"
    assert "PARFRG0000000000000000" in task_uuids, \
        "untitled fragment attached to a project should still be kept"

    print("OK: t=2 baseline kept, empty t=2 treated as deletion (later tr:false "
          "still wins), trashed hidden, titled fragment kept, empty fragment "
          "skipped, status/destination-only fragment skipped, typed/attached "
          "fragment kept")


if __name__ == "__main__":
    test_full_log_is_replayed()
    test_t2_and_fragments()
    print("\nAll pagination tests passed.")
