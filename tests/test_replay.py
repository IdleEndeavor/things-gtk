"""Offline tests for the sync replay + model. No network or credentials needed.

Run with:  python3 -m pytest tests/  (or)  python3 tests/test_replay.py
"""

import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thingsgnome.sync.client import ThingsReadClient, Entity
from thingsgnome.model import Database, Kind, Status, Destination
from thingsgnome.sync import protocol as P


def ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def build_synthetic_state():
    """Simulate replaying a history log via the same _apply path the client uses."""
    entities: dict[str, Entity] = {}
    now = time.time()
    today = date.today()

    def new(uuid, etype, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": etype, "p": payload})

    def edit(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_EDIT, "e": P.ENTITY_TASK, "p": payload})

    AREA = "AREA000000000000000000"
    PROJ = "PROJ000000000000000000"
    HEAD = "HEAD000000000000000000"
    T1 = "TASK100000000000000000"
    T2 = "TASK200000000000000000"
    T3 = "TASK300000000000000000"
    CL1 = "CHK1000000000000000000"

    # Area (entity type unknown in the wild -> use a name starting with "Area")
    new(AREA, "Area3", {"tt": "🏍️ Motorcycle", "ix": 0})

    # Project inside the area
    new(PROJ, P.ENTITY_TASK, {
        "tt": "Get A2 licence", "tp": P.TYPE_PROJECT, "ss": P.STATUS_TODO,
        "st": P.DEST_ANYTIME, "ar": [AREA], "cd": now, "md": now,
        "nt": {"_t": "tx", "ch": 0, "v": "Book the CBT first.", "t": 1}, "ix": 0,
    })

    # Heading inside the project
    new(HEAD, P.ENTITY_TASK, {"tt": "Paperwork", "tp": P.TYPE_HEADING, "pr": [PROJ], "ix": 0})

    # Task due today, inside project under heading, with a checklist
    new(T1, P.ENTITY_TASK, {
        "tt": "Send licence application", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
        "st": P.DEST_ANYTIME, "pr": [PROJ], "agr": [HEAD],
        "dd": ts(today), "tg": ["urgent"], "cd": now, "md": now, "ix": 0,
    })
    new(CL1, P.ENTITY_CHECKLIST, {"tt": "Photocopy passport", "ss": P.STATUS_COMPLETE, "ts": [T1], "ix": 0})

    # Inbox task, later edited to be scheduled in the future (-> Upcoming)
    new(T2, P.ENTITY_TASK, {"tt": "Idea: chrome ext tweak", "tp": P.TYPE_TASK,
                            "ss": P.STATUS_TODO, "st": P.DEST_INBOX, "cd": now, "md": now, "ix": 1})
    edit(T2, {"st": P.DEST_ANYTIME, "sr": ts(today + timedelta(days=5)), "ar": [AREA]})

    # Completed task -> Logbook
    new(T3, P.ENTITY_TASK, {"tt": "Watch Scott Pilgrim", "tp": P.TYPE_TASK,
                            "ss": P.STATUS_COMPLETE, "st": P.DEST_ANYTIME, "ar": [AREA],
                            "sp": ts(today), "cd": now, "md": now, "ix": 2})

    return entities


def test_replay_and_model():
    entities = build_synthetic_state()
    db = Database.from_entities(entities)

    assert len(db.areas) == 1, "expected one area"
    assert db.areas[0].title == "🏍️ Motorcycle"
    assert len(db.projects) == 1
    assert db.projects[0].title == "Get A2 licence"
    assert db.projects[0].notes == "Book the CBT first."
    assert db.projects[0].area_id == db.areas[0].uuid

    # 3 tasks (heading is structural, not a task)
    assert len(db.tasks) == 3, f"expected 3 tasks, got {len(db.tasks)}"

    by_title = {t.title: t for t in db.tasks}
    t1 = by_title["Send licence application"]
    assert t1.heading_title == "Paperwork"
    assert t1.checklist == [("Photocopy passport", True)]
    assert t1.tags == ["urgent"]
    assert t1.project_id == db.projects[0].uuid

    # the EDIT delta must have been applied to T2
    t2 = by_title["Idea: chrome ext tweak"]
    assert t2.destination is Destination.ANYTIME
    assert t2.scheduled == date.today() + timedelta(days=5)

    print("OK: replay + model build")


def test_smart_lists():
    db = Database.from_entities(build_synthetic_state())
    lists = db.categorize()
    titles = lambda L: sorted(t.title for t in L)

    assert "Send licence application" in titles(lists.today), titles(lists.today)
    assert "Idea: chrome ext tweak" in titles(lists.upcoming), titles(lists.upcoming)
    assert "Watch Scott Pilgrim" in titles(lists.logbook), titles(lists.logbook)
    assert lists.inbox == [], "nothing should be left in inbox"
    print("OK: smart-list categorisation")


def test_someday_wins_over_stale_scheduled_date():
    """Found on a real account: moving a task to Someday doesn't clear a
    pre-existing `scheduled_date` server-side. `categorize()` used to check the
    (now stale) date before checking destination, so an explicit "move to
    Someday" was silently overridden and the task kept showing in Today
    forever. A deadline (`dd`) due exactly today, unlike a stale
    scheduled_date, must still win (deadlines follow the same exactly-today
    rule as scheduled_date -- see test_today_is_exactly_today_not_any_past_date)."""
    entities: dict[str, Entity] = {}
    today = date.today()

    def new(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": P.ENTITY_TASK, "p": payload})

    def edit(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_EDIT, "e": P.ENTITY_TASK, "p": payload})

    STALE_SOMEDAY = "STALE0000000000000000"
    SOMEDAY_WITH_DEADLINE = "SDDL000000000000000000"

    new(STALE_SOMEDAY, {"tt": "Redesign site", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                         "st": P.DEST_ANYTIME, "sr": ts(today - timedelta(days=300)), "ix": 0})
    edit(STALE_SOMEDAY, {"st": P.DEST_SOMEDAY})  # moved to Someday; sr left stale

    new(SOMEDAY_WITH_DEADLINE, {"tt": "Someday but has a deadline", "tp": P.TYPE_TASK,
                                 "ss": P.STATUS_TODO, "st": P.DEST_SOMEDAY,
                                 "dd": ts(today), "ix": 1})

    db = Database.from_entities(entities)
    lists = db.categorize()

    someday_titles = {t.title for t in lists.someday}
    today_titles = {t.title for t in lists.today}

    assert "Redesign site" in someday_titles, "explicit Someday must win over a stale scheduled_date"
    assert "Redesign site" not in today_titles

    assert "Someday but has a deadline" in today_titles, \
        "an actual deadline must still pull a Someday task into Today"

    print("OK: Someday destination wins over a stale scheduled_date, but a real deadline still wins")


def test_export_format():
    data = {
        "exported_at": "1 Jan 2026",
        "areas": [{"id": "a1", "title": "Studies"}],
        "projects": [{"id": "p1", "title": "Dissertation", "parent_id": "a1",
                      "status": "Open", "notes": "", "deadline": "1 December 2026"}],
        "todos": [
            {"id": "t1", "title": "Outline chapter 1", "parent_id": "p1",
             "status": "Open", "start": "On Date", "start_date": "1 January 2020",
             "tags": "writing", "is_inbox": "No", "heading": "Drafting"},
            {"id": "t2", "title": "Random thought", "parent_id": "", "status": "Open",
             "is_inbox": "Yes", "start": ""},
        ],
    }
    db = Database.from_export(data)
    assert len(db.areas) == 1 and len(db.projects) == 1 and len(db.tasks) == 2
    lists = db.categorize()
    # "1 January 2020" is long past -- Today is scheduled-for-exactly-today (or
    # an overdue deadline), not "any scheduled date in the past" (confirmed
    # against a real account: see the comment in Database.categorize()), so a
    # stale scheduled date from a Shortcuts export lands in Anytime, not Today.
    assert any(t.title == "Outline chapter 1" for t in lists.anytime), lists.anytime
    assert any(t.title == "Random thought" for t in lists.inbox)
    print("OK: Shortcuts export import")


def test_today_is_exactly_today_not_any_past_date():
    """Confirmed directly against a real account and the user's own phone
    (not just log inspection): Today is "scheduled_date == today", an overdue
    deadline, or the "waiting" instance of a repeat -- NOT "any task whose
    scheduled_date happens to be <= today". A years-old scheduled-but-never-
    completed one-off task just sits in Anytime; an overdue repeat instance
    keeps nagging in Today regardless of staleness; a repeat instance with a
    FUTURE date is a normal Upcoming item, not "waiting"."""
    entities: dict[str, Entity] = {}
    today = date.today()

    def new(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": P.ENTITY_TASK, "p": payload})

    ONE_OFF_TODAY = "OOTODAY000000000000000"
    ONE_OFF_STALE = "OOSTALE00000000000000"
    # Two distinct repeat chains (different templates) so collapsing doesn't
    # reduce these to one instance before categorize() ever sees them --
    # collapsing itself is covered by test_recurring_instance_collapsing.
    REPEAT_TPL_A = "RPTPLA000000000000000"
    REPEAT_OVERDUE = "RPOVER000000000000000"
    REPEAT_TPL_B = "RPTPLB000000000000000"
    REPEAT_FUTURE = "RPFUT0000000000000000"
    REPEAT_TPL_C = "RPTPLC000000000000000"
    REPEAT_WAITING_SOMEDAY = "RPWSD0000000000000000"

    new(ONE_OFF_TODAY, {"tt": "Do it today", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                         "st": P.DEST_ANYTIME, "sr": ts(today), "ix": 0})
    new(ONE_OFF_STALE, {"tt": "Old one-off, never completed", "tp": P.TYPE_TASK,
                         "ss": P.STATUS_TODO, "st": P.DEST_ANYTIME,
                         "sr": ts(today - timedelta(days=600)), "ix": 1})
    OVERDUE_DEADLINE = "OODDLN000000000000000"
    new(OVERDUE_DEADLINE, {"tt": "Deadline passed months ago", "tp": P.TYPE_TASK,
                            "ss": P.STATUS_TODO, "st": P.DEST_ANYTIME,
                            "dd": ts(today - timedelta(days=90)), "ix": 10})
    # icc (instance_creation_count) > 0 on the template is what marks a chain
    # as genuinely active when only one instance of it survives in the log --
    # see _collapse_recurring_instances. Real "Get a Haircut" had icc=6 with
    # only 1 instance actually observed (compaction), exactly this shape.
    new(REPEAT_TPL_A, {"tt": "Chore A", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                        "st": P.DEST_ANYTIME, "rr": {"tp": 1}, "icc": 6, "ix": 2})
    new(REPEAT_OVERDUE, {"tt": "Chore", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                          "st": P.DEST_ANYTIME, "rt": [REPEAT_TPL_A],
                          "sr": ts(today - timedelta(days=400)), "ix": 3})
    new(REPEAT_TPL_B, {"tt": "Chore B", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                        "st": P.DEST_ANYTIME, "rr": {"tp": 1}, "icc": 34, "ix": 4})
    new(REPEAT_FUTURE, {"tt": "Change the pillowcase", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                         "st": P.DEST_ANYTIME, "rt": [REPEAT_TPL_B],
                         "sr": ts(today + timedelta(days=5)), "ix": 5})
    new(REPEAT_TPL_C, {"tt": "Chore C", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                        "st": P.DEST_ANYTIME, "rr": {"tp": 1}, "icc": 10, "ix": 6})
    # A "waiting" instance carrying destination=SOMEDAY (confirmed on a real
    # account: an overdue/no-fixed-date repeat instance commonly has this
    # destination, same as a genuine Someday task) -- must still land in
    # Today, not get silently absorbed into Someday.
    new(REPEAT_WAITING_SOMEDAY, {"tt": "Waiting, but destination is Someday", "tp": P.TYPE_TASK,
                                  "ss": P.STATUS_TODO, "st": P.DEST_SOMEDAY, "rt": [REPEAT_TPL_C],
                                  "sr": ts(today - timedelta(days=90)), "ix": 7})

    # Dormant chain: an `rt` reference to a template that was defined but
    # never actually generated more instances (icc=0, exactly one instance
    # ever seen) -- confirmed on a real account this is common and must NOT
    # get the perpetual "waiting" treatment, or plain one-off tasks that
    # happen to carry a stale `rt` flood into Today alongside genuine repeats.
    DORMANT_TPL = "RPTPLD000000000000000"
    DORMANT_INSTANCE = "RPDORM000000000000000"
    new(DORMANT_TPL, {"tt": "Never actually repeated", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                       "st": P.DEST_ANYTIME, "rr": {"tp": 1}, "icc": 0, "ix": 8})
    new(DORMANT_INSTANCE, {"tt": "Dormant one-off", "tp": P.TYPE_TASK, "ss": P.STATUS_TODO,
                            "st": P.DEST_ANYTIME, "rt": [DORMANT_TPL],
                            "sr": ts(today - timedelta(days=500)), "ix": 9})

    db = Database.from_entities(entities)
    lists = db.categorize()
    today_titles = {t.title for t in lists.today}
    anytime_titles = {t.title for t in lists.anytime}
    upcoming_titles = {t.title for t in lists.upcoming}
    someday_titles = {t.title for t in lists.someday}

    assert "Do it today" in today_titles
    assert "Old one-off, never completed" in anytime_titles
    assert "Old one-off, never completed" not in today_titles
    assert "Chore" in today_titles, "an overdue repeat instance must keep nagging in Today"
    assert "Change the pillowcase" in upcoming_titles, \
        "a repeat instance with a future date is a normal Upcoming item, not 'waiting'"
    assert "Change the pillowcase" not in today_titles
    assert "Waiting, but destination is Someday" in today_titles, \
        "a waiting repeat instance must win over its own Someday-ish destination"
    assert "Waiting, but destination is Someday" not in someday_titles

    assert "Dormant one-off" in anytime_titles, \
        "an rt-linked task whose template never actually generated instances (icc=0) " \
        "must be treated as an ordinary dated task, not a perpetual 'waiting' repeat"
    assert "Dormant one-off" not in today_titles

    assert "Deadline passed months ago" in anytime_titles, \
        "a deadline merely in the past (not exactly today) must not force Today either " \
        "-- same exactly-today rule as scheduled_date, inferred by symmetry"
    assert "Deadline passed months ago" not in today_titles

    print("OK: Today is exactly-today/overdue-deadline/waiting-repeat, not any past scheduled date, "
          "and a dormant rt reference (icc=0) doesn't get the waiting treatment")


def test_recurring_instance_collapsing():
    """Things Cloud pre-materializes several instances of a repeat, all sharing
    `rt` back to a template entity that carries its own `rr`. Found by
    inspecting a real account: the real app shows only one open occurrence at a
    time and never the template itself. See model.py's
    `_collapse_recurring_instances` / `_select_current_recurring_instance` --
    the "closest to today, preferring overdue" tie-break there is a documented
    heuristic (REVERSE-ENGINEERING.md §8), not a confirmed rule."""
    entities: dict[str, Entity] = {}
    today = date.today()

    def new(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": P.ENTITY_TASK, "p": payload})

    TITLE = "Take out recycling"
    TPL = "RTPL000000000000000000"
    I_OLD = "RIOLD00000000000000000"      # open, ~2 years overdue
    I_NEAR = "RINEAR0000000000000000"     # open, a few days overdue -- should win
    I_FUTURE = "RIFUT00000000000000000"   # open, a week in the future
    I_DONE1 = "RIDONE1000000000000000"    # completed instance
    I_DONE2 = "RIDONE2000000000000000"    # completed instance
    UNRELATED = "UNREL0000000000000000"   # same title, no rt link -- must be untouched

    common = dict(tp=P.TYPE_TASK, ss=P.STATUS_TODO, st=P.DEST_ANYTIME, tt=TITLE)
    new(TPL, {**common, "rr": {"tp": 0, "fu": 256}, "ix": 0})
    new(I_OLD, {**common, "rt": [TPL], "sr": ts(today - timedelta(days=700)), "ix": 1})
    new(I_NEAR, {**common, "rt": [TPL], "sr": ts(today - timedelta(days=3)), "ix": 2})
    new(I_FUTURE, {**common, "rt": [TPL], "sr": ts(today + timedelta(days=7)), "ix": 3})
    new(I_DONE1, {
        "tt": TITLE, "tp": P.TYPE_TASK, "ss": P.STATUS_COMPLETE, "st": P.DEST_ANYTIME,
        "rt": [TPL], "sr": ts(today - timedelta(days=14)), "sp": ts(today - timedelta(days=14)), "ix": 4,
    })
    new(I_DONE2, {
        "tt": TITLE, "tp": P.TYPE_TASK, "ss": P.STATUS_COMPLETE, "st": P.DEST_ANYTIME,
        "rt": [TPL], "sr": ts(today - timedelta(days=21)), "sp": ts(today - timedelta(days=21)), "ix": 5,
    })
    new(UNRELATED, {**common, "ix": 6})

    db = Database.from_entities(entities)
    uuids = {t.uuid for t in db.tasks}

    assert TPL not in uuids, "the repeat template itself must not appear as a task"
    assert I_OLD not in uuids, "the stale (~2yr overdue) instance must be collapsed away"
    assert I_NEAR in uuids, "the open instance closest to today must be the one kept"
    assert I_FUTURE not in uuids, "the not-yet-due future instance must be collapsed away"
    assert I_DONE1 in uuids and I_DONE2 in uuids, "completed instances all remain (Logbook history)"
    assert UNRELATED in uuids, "a same-titled task with no rt link must be left untouched"

    print("OK: recurring-instance collapsing keeps one open instance closest to "
          "today, drops the template and stale/future instances, keeps completed "
          "instances, and leaves unrelated same-titled tasks alone")


def test_after_completion_repeat_computes_next_occurrence():
    """Confirmed live against a real account (REVERSE-ENGINEERING.md §9.7): for
    an active after-completion repeat (rr.tp == 1), the server never
    materializes the next occurrence as its own Task entity -- only completed
    instances and stale open ones show up in the log, even immediately after a
    fresh sync. _collapse_recurring_instances must compute it itself: last
    completion_date + fa scaled by fu (the legacy NSCalendarUnit bitmask
    confirmed in §6: 4 year, 8 month, 16 day, 256 week)."""
    entities: dict[str, Entity] = {}
    today = date.today()

    def new(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": P.ENTITY_TASK, "p": payload})

    TITLE = "Change the pillowcase"
    TPL = "ACTPL000000000000000000"
    STALE_OPEN = "ACOPEN00000000000000000"
    DONE = "ACDONE0000000000000000"

    common = dict(tp=P.TYPE_TASK, ss=P.STATUS_TODO, st=P.DEST_ANYTIME, tt=TITLE)
    # after-completion repeat, every 7 days -- fu:16 is "day" (§6)
    new(TPL, {**common, "rr": {"tp": 1, "fu": 16, "fa": 7}, "icc": 5, "ix": 0})
    new(STALE_OPEN, {**common, "rt": [TPL], "sr": ts(today - timedelta(days=200)), "ix": 1})
    completed_on = today - timedelta(days=3)
    new(DONE, {
        "tt": TITLE, "tp": P.TYPE_TASK, "ss": P.STATUS_COMPLETE, "st": P.DEST_ANYTIME,
        "rt": [TPL], "sr": ts(completed_on), "sp": ts(completed_on), "ix": 2,
    })

    db = Database.from_entities(entities)
    survivor = next(t for t in db.tasks if t.uuid == STALE_OPEN)
    expected = completed_on + timedelta(days=7)
    assert survivor.scheduled == expected, (
        f"expected computed next occurrence {expected}, got {survivor.scheduled}"
    )
    assert survivor.repeat_rule == "Repeats 7 days after completion", (
        f"got {survivor.repeat_rule!r}"
    )

    print("OK: after-completion repeat's next occurrence is computed from the "
          "last completion + interval, not left on a stale server-materialized "
          "instance, and its repeat_rule text is built literally (7 days, not "
          "1 week)")


def test_after_completion_repeat_never_regresses_a_correct_instance():
    """A chain the server DID materialize correctly (a genuine future-dated
    open instance newer than what the after-completion formula would compute)
    must never be regressed by the computed override -- only apply it when
    it's later than what the log already has. Covers both shapes: a single
    correctly-materialized open instance, and -- the case that actually
    matters, since _select_current_recurring_instance prefers overdue over
    future and would otherwise drop the correct instance before the override
    even sees it -- a chain with BOTH a stale instance and a genuinely future
    one open at once (confirmed to happen: Things Cloud pre-materializes
    several instances of a repeat, per _select_current_recurring_instance's
    own docstring)."""
    today = date.today()

    def new(entities, uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": P.ENTITY_TASK, "p": payload})

    TITLE = "Water the plants"
    TPL = "NRTPL000000000000000000"
    FUTURE_OPEN = "NROPEN0000000000000000"
    STALE_OPEN = "NRSTAL0000000000000000"
    DONE = "NRDONE0000000000000000"
    common = dict(tp=P.TYPE_TASK, ss=P.STATUS_TODO, st=P.DEST_ANYTIME, tt=TITLE)
    genuinely_future = today + timedelta(days=30)
    completed_on = today - timedelta(days=3)  # would compute to today + 4 days

    # Shape 1: only the correct future instance is open.
    entities: dict[str, Entity] = {}
    new(entities, TPL, {**common, "rr": {"tp": 1, "fu": 16, "fa": 7}, "icc": 5, "ix": 0})
    new(entities, FUTURE_OPEN, {**common, "rt": [TPL], "sr": ts(genuinely_future), "ix": 1})
    new(entities, DONE, {
        "tt": TITLE, "tp": P.TYPE_TASK, "ss": P.STATUS_COMPLETE, "st": P.DEST_ANYTIME,
        "rt": [TPL], "sr": ts(completed_on), "sp": ts(completed_on), "ix": 2,
    })
    db = Database.from_entities(entities)
    survivor = next(t for t in db.tasks if t.uuid == FUTURE_OPEN)
    assert survivor.scheduled == genuinely_future, (
        f"a correctly server-materialized future date must not be overridden "
        f"by an older computed one, got {survivor.scheduled}"
    )

    # Shape 2: both a stale AND a correct future instance are open at once --
    # _select_current_recurring_instance picks the stale one as the survivor
    # (prefers due-or-overdue), so the override must still compare against
    # the future instance's date before deciding to apply, not just the
    # chosen survivor's own (stale) date.
    entities = {}
    new(entities, TPL, {**common, "rr": {"tp": 1, "fu": 16, "fa": 7}, "icc": 5, "ix": 0})
    stale_date = today - timedelta(days=200)
    new(entities, STALE_OPEN, {**common, "rt": [TPL], "sr": ts(stale_date), "ix": 1})
    new(entities, FUTURE_OPEN, {**common, "rt": [TPL], "sr": ts(genuinely_future), "ix": 2})
    new(entities, DONE, {
        "tt": TITLE, "tp": P.TYPE_TASK, "ss": P.STATUS_COMPLETE, "st": P.DEST_ANYTIME,
        "rt": [TPL], "sr": ts(completed_on), "sp": ts(completed_on), "ix": 3,
    })
    db = Database.from_entities(entities)
    survivors = [t for t in db.tasks if t.uuid in (STALE_OPEN, FUTURE_OPEN)]
    assert len(survivors) == 1, f"expected exactly one open survivor, got {len(survivors)}"
    assert survivors[0].scheduled == stale_date, (
        f"the stale instance is selected as survivor (closest-to-today "
        f"heuristic), and since a genuinely future instance also exists in "
        f"this chain, the computed date must NOT override it -- got "
        f"{survivors[0].scheduled}"
    )

    print("OK: a genuinely future server-materialized instance is never "
          "regressed by the after-completion computed date, including when "
          "it coexists with a stale instance in the same chain")


def test_fixed_schedule_repeat_ignores_completion_formula():
    """rr.tp == 0 (fixed-schedule, not after-completion) must never trigger
    the completion-date arithmetic -- it's meaningless for a fixed calendar
    repeat and the existing stale-open-instance behavior must be untouched."""
    entities: dict[str, Entity] = {}
    today = date.today()

    def new(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": P.ENTITY_TASK, "p": payload})

    TITLE = "Take out recycling"
    TPL = "FSTPL000000000000000000"
    STALE_OPEN = "FSOPEN0000000000000000"
    DONE = "FSDONE0000000000000000"

    common = dict(tp=P.TYPE_TASK, ss=P.STATUS_TODO, st=P.DEST_ANYTIME, tt=TITLE)
    new(TPL, {**common, "rr": {"tp": 0, "fu": 256, "fa": 1}, "icc": 5, "ix": 0})
    stale_date = today - timedelta(days=200)
    new(STALE_OPEN, {**common, "rt": [TPL], "sr": ts(stale_date), "ix": 1})
    completed_on = today - timedelta(days=3)
    new(DONE, {
        "tt": TITLE, "tp": P.TYPE_TASK, "ss": P.STATUS_COMPLETE, "st": P.DEST_ANYTIME,
        "rt": [TPL], "sr": ts(completed_on), "sp": ts(completed_on), "ix": 2,
    })

    db = Database.from_entities(entities)
    survivor = next(t for t in db.tasks if t.uuid == STALE_OPEN)
    assert survivor.scheduled == stale_date, (
        f"fixed-schedule repeats must be left exactly as the log has them, "
        f"got {survivor.scheduled}"
    )
    assert survivor.repeat_rule == "Repeats every week", f"got {survivor.repeat_rule!r}"

    print("OK: a fixed-schedule repeat (tp:0) is untouched by the "
          "after-completion computed-date logic, and its repeat_rule text "
          "says 'every week' (fa:1 singular, no 'every 1 weeks')")


def test_repeat_rule_text_matches_confirmed_real_examples():
    """Not a computed-date test -- just the repeat_rule text itself, checked
    against the two literal real-account examples confirmed in
    REVERSE-ENGINEERING.md §6/§9.7 ("Get a Haircut": fu:8 fa:3 -> "Repeats 3
    months after completion"; "Create and sort the budget": fu:8 fa:1 ->
    "Repeats 1 month after completion"), plus an untested-on-this-account
    fixed-schedule case (tp:0) to confirm the "every N units" phrasing and
    that an unrecognized/incomplete rule renders no text at all rather than
    guessing."""
    entities: dict[str, Entity] = {}

    def new(uuid, payload):
        ThingsReadClient._apply(entities, uuid, {"t": P.UPDATE_NEW, "e": P.ENTITY_TASK, "p": payload})

    def one_active_repeat(title, tpl_uuid, inst_uuid, rr):
        common = dict(tp=P.TYPE_TASK, ss=P.STATUS_TODO, st=P.DEST_ANYTIME, tt=title)
        new(tpl_uuid, {**common, "rr": rr, "icc": 5, "ix": 0})
        new(inst_uuid, {**common, "rt": [tpl_uuid], "ix": 1})

    one_active_repeat("Get a Haircut", "RTHC00000000000000000000", "RIHC00000000000000000000",
                       {"tp": 1, "fu": 8, "fa": 3})
    one_active_repeat("Create and sort the budget", "RTBG00000000000000000000", "RIBG00000000000000000000",
                       {"tp": 1, "fu": 8, "fa": 1})
    one_active_repeat("Water every 2 years", "RTYR00000000000000000000", "RIYR00000000000000000000",
                       {"tp": 0, "fu": 4, "fa": 2})
    one_active_repeat("Unknown unit repeat", "RTUK00000000000000000000", "RIUK00000000000000000000",
                       {"tp": 1, "fu": 999, "fa": 3})

    db = Database.from_entities(entities)
    by_uuid = {t.uuid: t for t in db.tasks}

    assert by_uuid["RIHC00000000000000000000"].repeat_rule == "Repeats 3 months after completion"
    assert by_uuid["RIBG00000000000000000000"].repeat_rule == "Repeats 1 month after completion"
    assert by_uuid["RIYR00000000000000000000"].repeat_rule == "Repeats every 2 years"
    assert by_uuid["RIUK00000000000000000000"].repeat_rule == "", (
        "an unrecognized fu must render no text at all, never a guess"
    )

    print("OK: repeat_rule text matches the two real-account examples "
          "confirmed in REVERSE-ENGINEERING.md exactly, and an unrecognized "
          "unit renders nothing rather than guessing")


if __name__ == "__main__":
    test_replay_and_model()
    test_smart_lists()
    test_someday_wins_over_stale_scheduled_date()
    test_today_is_exactly_today_not_any_past_date()
    test_export_format()
    test_recurring_instance_collapsing()
    test_after_completion_repeat_computes_next_occurrence()
    test_after_completion_repeat_never_regresses_a_correct_instance()
    test_fixed_schedule_repeat_ignores_completion_formula()
    test_repeat_rule_text_matches_confirmed_real_examples()
    print("\nAll tests passed.")
