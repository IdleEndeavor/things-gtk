"""Domain model + smart-list categorisation.

Two inputs are supported and normalise to the same model:
  * `Database.from_entities(...)` - replayed Things Cloud entities (live sync), and
  * `Database.from_export(...)`   - the JSON your iOS Shortcut already produces.

The smart-list logic (Inbox / Today / Upcoming / Anytime / Someday / Logbook) is a
faithful port of the rules in your original things-viewer.html, adapted to operate on
real datetimes instead of pre-formatted strings.
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

from .sync.client import Entity
from .sync import protocol as P


class Status(str, Enum):
    OPEN = "Open"
    COMPLETED = "Completed"
    CANCELED = "Canceled"


class Destination(str, Enum):
    INBOX = "Inbox"
    ANYTIME = "Anytime"
    SOMEDAY = "Someday"


class Kind(str, Enum):
    TASK = "task"
    PROJECT = "project"
    HEADING = "heading"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ts_to_date(ts) -> date | None:
    if ts in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).date()
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _date_to_ts(d: date | None) -> int | None:
    """Exact inverse of `_ts_to_date`: midnight UTC of `d` as a unix timestamp.

    Used when building EDIT deltas (`sr`/`dd`) so a value we send and then read
    back through `_ts_to_date` round-trips to the same date. Do not swap in a
    "local midnight" or "now" formula here -- that desyncs from what the reader
    expects and shows up as a silent off-by-one-day, not an error.
    """
    if d is None:
        return None
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _add_calendar_units(d: date, fu, fa) -> date | None:
    """Advance `d` by `fa` units of `fu`.

    `fu` is the legacy `NSCalendarUnit` bitmask Things' `recurrence_rule.fu`
    carries -- confirmed against a real account in REVERSE-ENGINEERING.md §6:
    4 year, 8 month, 16 day, 256 week. Manual month/year arithmetic with
    day-clamping since this project has no dateutil dependency. Returns None
    for an unrecognized unit or missing amount, rather than guessing.
    """
    if not fu or not fa:
        return None
    if fu == 16:
        return d + timedelta(days=fa)
    if fu == 256:
        return d + timedelta(weeks=fa)
    if fu == 8:
        total_months = d.year * 12 + (d.month - 1) + fa
        year, month0 = divmod(total_months, 12)
        month = month0 + 1
        day = min(d.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    if fu == 4:
        year = d.year + fa
        day = min(d.day, calendar.monthrange(year, d.month)[1])
        return date(year, d.month, day)
    return None


_CALENDAR_UNIT_NAMES = {4: "year", 8: "month", 16: "day", 256: "week"}


def _repeat_rule_text(rr: dict) -> str:
    """Human-readable repeat description built from a template's recurrence_rule.

    Only `tp` (0 fixed-schedule / 1 after-completion), `fu` (unit) and `fa`
    (amount) are used -- all three confirmed against a real account (§6,
    §9.7). Deliberately does NOT touch `of` (the exact weekday/day-of-month
    anchor a fixed-schedule repeat fires on): every fixed-schedule (`tp:0`)
    repeat found on a real account had its `recurrence_rule` compacted away
    entirely, so there was no live example to decode `of` against, and a
    wrong guess there is exactly what REVERSE-ENGINEERING.md §8 flags as
    highest-risk. This only ever renders the unit/amount, never a specific
    day -- "Repeats every 3 months", not "Repeats on the 14th".

    Units are rendered literally, not normalized (`fu:16, fa:7` renders "7
    days", never "1 week") -- that's the only thing the stored data supports,
    since `fu` records whichever unit the rule was created with. Suggestive
    but NOT confirmed that the real Things apps do the same: a hand-typed
    account dump (not a screenshot, so not literal app copy) describes one
    chain as "7 days" and a different chain as "1 week", but that second
    chain's template is fully compacted on this account, so its `fu` can't be
    directly checked either way. Treat "no normalization" as this
    implementation's own reasonable choice, not a reverse-engineered fact.
    """
    fu, fa, tp = rr.get("fu"), rr.get("fa"), rr.get("tp")
    unit = _CALENDAR_UNIT_NAMES.get(fu)
    if not unit or not fa:
        return ""
    plural = "" if fa == 1 else "s"
    if tp == 1:
        return f"Repeats {fa} {unit}{plural} after completion"
    every = unit if fa == 1 else f"{fa} {unit}{plural}"
    return f"Repeats every {every}"


def _note_text(note) -> str:
    """Extract note text from Things' two note encodings.

    A creation/baseline note is {"_t":"tx","v":<full text>,...}. A later edit can
    instead send an operational patch {"_t":"tx","ps":[{"r":<replacement>,...}]}.
    We use the full value when present, else best-effort join the patch
    replacements. (Full OT reconstruction isn't done; see REVERSE-ENGINEERING.md.)
    """
    if note is None:
        return ""
    if isinstance(note, str):
        return note
    if isinstance(note, dict):
        v = note.get("v", note.get("value"))
        if isinstance(v, str) and v:
            return v
        ps = note.get("ps")
        if isinstance(ps, list):
            return "".join(
                p.get("r", "") for p in ps if isinstance(p, dict) and isinstance(p.get("r"), str)
            )
    return ""


def _status_from_int(ss) -> Status:
    if ss == P.STATUS_COMPLETE:
        return Status.COMPLETED
    if ss == P.STATUS_CANCELLED:
        return Status.CANCELED
    return Status.OPEN


def _select_current_recurring_instance(open_instances: list["Task"], today: date) -> "Task":
    """Which of several OPEN instances of one recurring task to treat as "the"
    task, when several exist in the log at once.

    ⚠️ HEURISTIC, not confirmed against the real Mac/iOS app -- see
    REVERSE-ENGINEERING.md §8 item 8 for exactly what to check. Things Cloud
    pre-materializes several upcoming (and
    sometimes long-overdue, going back years on an old repeat) instances of a
    repeat in the log, all sharing `repeating_template`, but real Things only
    ever shows one at a time. We pick the open instance whose date is closest
    to today, preferring a due-or-overdue one over a not-yet-due future one --
    but the *closest* overdue one, not the *oldest* overdue one, so an
    abandoned repeat from years ago doesn't resurface dated in the past
    forever. If this rule turns out to be wrong once captured, it's isolated
    here and in `_collapse_recurring_instances` -- nothing else needs to change.
    """
    dated = [t for t in open_instances if (t.scheduled or t.deadline)]
    if not dated:
        return open_instances[0]
    due_or_overdue = [t for t in dated if (t.scheduled or t.deadline) <= today]
    pool = due_or_overdue or dated
    return min(pool, key=lambda t: abs(((t.scheduled or t.deadline) - today).days))


def _collapse_recurring_instances(
    tasks: list["Task"], entities: dict[str, "Entity"] | None = None,
) -> list["Task"]:
    """Drop all but one OPEN instance of each recurring task chain, and flag
    the survivor as `repeat_is_active` when the chain is a genuinely-running
    repeat (see below) so categorize() can give it the perpetual "waiting in
    Today" treatment.

    Tasks that don't share a `repeating_template_id` are returned untouched --
    this only affects entities Things Cloud itself linked together, never
    same-titled tasks a user happened to create separately (confirmed by
    inspecting a real account: same-titled duplicates with no `rt` link are
    real, distinct, user-created items). Closed (completed/cancelled/trashed)
    instances are never dropped -- they still belong in Logbook.

    Not every `rt`-linked task is an active repeat. Confirmed on a real
    account: several one-off tasks carried a `repeating_template` reference
    to a template whose own `instance_creation_count` was 0 and which only
    ever produced that single instance -- these are dormant/never-actually-
    repeating chains, and treating them as perpetually "waiting" pulled tasks
    the user had NOT listed as waiting into Today. Genuinely active chains
    (confirmed: `icc` actively incrementing over time, or several distinct
    instances observed) get the waiting treatment; dormant ones are treated
    as a normal dated task instead.

    An active chain can have ZERO open instances at all: confirmed on a real
    account for three separate repeats simultaneously -- each had exactly one
    referencing entity in the *entire* raw history log, and it was completed.
    The real app still shows these as "waiting" in Today, so the template
    itself (which does have a real title, unlike bare instances) becomes the
    stand-in placeholder in that case, instead of being dropped.

    For an active after-completion repeat (`recurrence_rule.tp == 1`), the
    survivor's `scheduled` date is also overridden with `last completion_date
    + fa` (scaled by `fu`) when that's later than what the log already has --
    confirmed live (REVERSE-ENGINEERING.md §9.7/§6) that the server never
    materializes that next occurrence as its own Task entity, so without this
    the survivor stays pinned to a stale, possibly long-overdue open instance
    even when the chain is actively advancing.

    This override is DISPLAY-ONLY: it exists purely to make categorize()/the
    UI show the same date the real Things apps show. The underlying entity's
    `scheduled_date` in the replayed log is untouched -- this only mutates the
    in-memory Task built for this render. If a future write path adds
    rescheduling (§6 lists it as confirmed-but-unimplemented), it must not
    read `task.scheduled` as if it were server-confirmed state and echo a
    computed date back as a write; it should go through the raw entity/log
    value instead.
    """
    by_template: dict[str, list[Task]] = {}
    for t in tasks:
        if t.repeating_template_id:
            by_template.setdefault(t.repeating_template_id, []).append(t)
    if not by_template:
        return tasks

    today = date.today()
    entities = entities or {}
    task_by_uuid = {t.uuid: t for t in tasks}
    # A task that IS a repeat's definition (its own uuid is referenced by
    # other tasks' repeating_template_id) is structural, not an occurrence --
    # drop it once an open instance exists to represent the chain, the same
    # way a heading is dropped in favor of surfacing heading_title on its
    # child tasks instead. It's kept as a placeholder when there's no open
    # instance to drop it in favor of (see docstring).
    drop: set[str] = set(by_template.keys())
    keep_uuid_active: dict[str, bool] = {}
    for template_id, group in by_template.items():
        template_ent = entities.get(template_id)
        tf = template_ent.fields if template_ent else {}
        icc = tf.get("instance_creation_count")
        # `instance_creation_start_date` (icsd) is ticked forward by ~1 day at
        # a time on a live chain (observed directly: dozens of consecutive
        # daily-incrementing EDITs on confirmed-active templates). A value
        # within the last month of today means the server is still actively
        # advancing it right now; a value frozen far in the past means it
        # stopped. This survives compaction better than `icc` -- confirmed:
        # one real active repeat had icc missing entirely but icsd == tomorrow,
        # while a confirmed-dormant one had icsd frozen over two years stale.
        icsd = _ts_to_date(tf.get("instance_creation_start_date"))
        icsd_is_fresh = bool(icsd) and (today - icsd).days <= 30
        is_active = (icc is not None and icc > 0) or len(group) > 1 or icsd_is_fresh

        # For an active after-completion repeat (rr.tp == 1), the server does
        # not keep a materialized "next occurrence" Task entity in the log at
        # all -- confirmed live, REVERSE-ENGINEERING.md §9.7: a chain's last
        # completion event was present in the log with none of its stated
        # after-completion interval reflected in any open instance. The real
        # Things apps must compute the next date themselves, the same way we
        # do here: last completion_date + fa scaled by fu (§6's confirmed
        # legacy-NSCalendarUnit table). Only fixed-schedule repeats (tp == 0)
        # skip this -- their instances are meaningfully server-materialized.
        computed_next: date | None = None
        rr = tf.get("recurrence_rule")
        # Human-readable "Repeats N unit(s) [after completion]" line -- only
        # for a genuinely active chain, and only when the template's rr
        # survived compaction (confirmed: 3 of 6 real repeating tasks on one
        # account still had it; the other 3 had a fully-compacted template
        # and simply show no repeat line, same data-loss pattern as §9.6/9.7).
        repeat_text = _repeat_rule_text(rr) if is_active and isinstance(rr, dict) else ""
        if is_active and isinstance(rr, dict) and rr.get("tp") == 1:
            completions = [t.completion_date for t in group if t.completion_date]
            if completions:
                computed_next = _add_calendar_units(
                    max(completions), rr.get("fu"), rr.get("fa")
                )

        open_in_group = [t for t in group if t.is_open and not t.trashed]
        if open_in_group:
            keep = (
                _select_current_recurring_instance(open_in_group, today)
                if len(open_in_group) > 1 else open_in_group[0]
            )
            drop.update(t.uuid for t in open_in_group if t.uuid != keep.uuid)
            keep_uuid_active[keep.uuid] = is_active
            # Only override when the computed date is more current than the
            # BEST date anywhere in this chain's open instances -- not just
            # the survivor's own date. _select_current_recurring_instance
            # prefers due-or-overdue over not-yet-due, so on a chain with one
            # stale instance and one genuinely future (server-materialized)
            # one, the future instance is what gets dropped here; comparing
            # only against the stale survivor would let this override discard
            # a correct server date instead of protecting it.
            best_known = max(
                (t.scheduled for t in open_in_group if t.scheduled), default=None
            )
            if computed_next and (not best_known or computed_next > best_known):
                keep.scheduled = computed_next
            keep.repeat_rule = repeat_text
        elif is_active and template_id in task_by_uuid:
            template_task = task_by_uuid[template_id]
            if template_task.is_open and not template_task.trashed:
                drop.discard(template_id)
                keep_uuid_active[template_id] = True
                if computed_next and (
                    not template_task.scheduled or computed_next > template_task.scheduled
                ):
                    template_task.scheduled = computed_next
                template_task.repeat_rule = repeat_text
                if not template_task.title.strip():
                    # The template's own title can be lost the same way its
                    # type/status can (its NEW event compacted away) -- borrow
                    # the title from any instance that referenced it, since
                    # they name the same conceptual repeating task.
                    titled = next((g.title for g in group if g.title.strip()), "")
                    if titled:
                        template_task.title = titled

    result = []
    for t in tasks:
        if t.uuid in drop:
            continue
        if t.uuid in keep_uuid_active:
            t.repeat_is_active = keep_uuid_active[t.uuid]
        result.append(t)
    return result


def _dest_from_int(st) -> Destination:
    if st == P.DEST_INBOX:
        return Destination.INBOX
    if st == P.DEST_SOMEDAY:
        return Destination.SOMEDAY
    return Destination.ANYTIME


_EMOJI_RE = re.compile(
    r"^(\U0001F300-\U0001FAFF|[\u2600-\u27BF]|\U0001F000-\U0001F9FF)\uFE0F?\s*"
)


def split_emoji(title: str) -> tuple[str, str]:
    """Return (emoji, clean_title). Areas in Things often start with an emoji."""
    if not title:
        return "", ""
    ch = title[0]
    if ord(ch) >= 0x2190:  # roughly: symbols and emoji range
        rest = title[1:]
        if rest.startswith("\uFE0F"):
            rest = rest[1:]
        return ch, rest.strip()
    return "", title


# --------------------------------------------------------------------------- #
# domain objects
# --------------------------------------------------------------------------- #
@dataclass
class Area:
    uuid: str
    title: str
    index: int = 0


@dataclass
class Task:
    uuid: str
    title: str
    kind: Kind = Kind.TASK
    status: Status = Status.OPEN
    destination: Destination = Destination.INBOX
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    deadline: date | None = None
    scheduled: date | None = None
    completion_date: date | None = None
    is_evening: bool = False
    trashed: bool = False
    project_id: str | None = None
    area_id: str | None = None
    heading_id: str | None = None
    heading_title: str = ""
    checklist: list[tuple[str, bool]] = field(default_factory=list)
    index: int = 0
    today_index: int = 0
    repeating_template_id: str | None = None
    # True only for an instance of a repeat chain that's actually still
    # generating instances (see _collapse_recurring_instances) -- lets
    # categorize() give it the perpetual "waiting in Today" treatment without
    # doing so for a one-off task that merely happens to carry a stale `rt`
    # reference to a repeat that was defined once and never actually ran.
    repeat_is_active: bool = False
    # Human-readable repeat description ("Repeats 3 months after completion"),
    # built from the template's recurrence_rule when repeat_is_active -- see
    # _repeat_rule_text. Empty when the template's rr was compacted away (no
    # data to build one from) or the chain isn't a genuinely active repeat.
    repeat_rule: str = ""

    # convenience -------------------------------------------------------------
    @property
    def parent_id(self) -> str | None:
        return self.project_id or self.area_id

    @property
    def is_open(self) -> bool:
        return self.status is Status.OPEN

    @property
    def is_closed(self) -> bool:
        return self.status in (Status.COMPLETED, Status.CANCELED)

    @property
    def is_inbox(self) -> bool:
        return self.destination is Destination.INBOX and not self.parent_id

    # write deltas ---------------------------------------------------------
    # EDIT payloads (two-letter wire keys) for the "mark" family, cross-confirmed
    # against two independent write implementations (disrupted/things-cloud-api's
    # TodoItem.status setter and evanpurkhiser/things3-cloud's mark.rs, whose own
    # unit test asserts these literal bodies). `sp` (completion_date) is sent as
    # the same now-timestamp as `md`, not a separately rounded int -- that's what
    # both references send and it's what a live "mark done" was tested against.
    def complete_delta(self) -> dict:
        now = time.time()
        return {"ss": P.STATUS_COMPLETE, "sp": now, "md": now}

    def uncomplete_delta(self) -> dict:
        # `sp` must be an explicit None, not omitted: a naive "drop empty fields"
        # filter would leave the old completion date in place server-side.
        return {"ss": P.STATUS_TODO, "sp": None, "md": time.time()}

    def cancel_delta(self) -> dict:
        now = time.time()
        return {"ss": P.STATUS_CANCELLED, "sp": now, "md": now}


# --------------------------------------------------------------------------- #
# database
# --------------------------------------------------------------------------- #
@dataclass
class SmartLists:
    inbox: list[Task]
    today: list[Task]
    upcoming: list[Task]
    anytime: list[Task]
    someday: list[Task]
    logbook: list[Task]


class Database:
    def __init__(
        self,
        areas: list[Area],
        projects: list[Task],
        tasks: list[Task],
        *,
        exported_at: str = "",
    ) -> None:
        self.areas = areas
        self.projects = projects          # Tasks with kind == PROJECT
        self.tasks = tasks                 # Tasks with kind == TASK
        self.exported_at = exported_at
        self._area_by_id = {a.uuid: a for a in areas}
        self._project_by_id = {p.uuid: p for p in projects}

    # -- lookups ---------------------------------------------------------------
    def area(self, uuid: str) -> Area | None:
        return self._area_by_id.get(uuid)

    def project(self, uuid: str) -> Task | None:
        return self._project_by_id.get(uuid)

    def projects_in_area(self, area_id: str) -> list[Task]:
        return sorted(
            (p for p in self.projects if p.area_id == area_id and not p.trashed),
            key=lambda p: p.index,
        )

    def tasks_in_project(self, project_id: str) -> list[Task]:
        return sorted(
            (t for t in self.tasks if t.project_id == project_id and not t.trashed),
            key=lambda t: t.index,
        )

    def tasks_in_area(self, area_id: str) -> list[Task]:
        return sorted(
            (t for t in self.tasks
             if t.area_id == area_id and not t.project_id and not t.trashed),
            key=lambda t: t.index,
        )

    def loose_projects(self) -> list[Task]:
        return [p for p in self.projects if not p.area_id and not p.trashed]

    # -- smart lists (ported from things-viewer.html) -------------------------
    def categorize(self) -> SmartLists:
        today = date.today()
        inbox, today_l, upcoming, anytime, someday, logbook = [], [], [], [], [], []
        for t in self.tasks:
            if t.trashed:
                continue
            if t.is_closed:
                logbook.append(t)
                continue
            if t.destination is Destination.INBOX and not t.parent_id:
                inbox.append(t)
                continue
            sched, due = t.scheduled, t.deadline
            # Confirmed against a real account + phone, both by inspection and
            # directly asked of the user (a "scheduled_date <= today" task and
            # its real phone state were compared one by one):
            #   - Today is scheduled_date == today exactly, deadline == today
            #     exactly, or the "waiting" instance of a repeat -- NOT any
            #     task whose scheduled_date OR deadline merely happens to be
            #     in the past. (The deadline == today rule is the one piece
            #     here inferred by symmetry with the scheduled_date rule,
            #     not directly confirmed by the user the way that one was --
            #     two real overdue-deadline tasks the user didn't list as
            #     current Today items is the evidence, not a direct answer.)
            #     A task scheduled weeks/months/years ago that was never
            #     completed just sits quietly in Anytime; Things does not keep
            #     re-surfacing it in Today forever. (One such task turned out
            #     to have been completed with no trace of that event anywhere
            #     in the readable history log -- Things Cloud appears to prune
            #     very old completions from what a fresh client can see at
            #     all, which this app has no way to recover; showing it in the
            #     large Anytime pile instead of prominently in Today is the
            #     best available fallback.)
            #   - A repeat instance (`repeating_template_id` set) that's
            #     overdue keeps showing in Today regardless of how stale its
            #     date is -- confirmed: a chore last done in 2024 still shows
            #     as "waiting" in Today today, un-dated in the UI. But a
            #     repeat instance with a FUTURE date is treated normally (an
            #     Upcoming item, not "waiting") -- confirmed: a repeat's next
            #     occurrence with a real future date shows in Upcoming, not
            #     Today.
            #   - A deadline == today still overrides Someday (a deadline is a
            #     binding commitment regardless of destination); a merely-past
            #     scheduled_date, or a merely-past deadline, does not.
            #   - A Someday task given a specific FUTURE date is Upcoming, not
            #     Someday -- confirmed: every real Upcoming item with a far
            #     future date (2029+) turned out to carry destination=SOMEDAY,
            #     not Anytime. Picking a date "graduates" it out of Someday for
            #     display even though the underlying destination field stays
            #     SOMEDAY. Only a stale *past* (or absent) date leaves it in
            #     Someday.
            #   - A "waiting" repeat instance (overdue or dateless) checks
            #     BEFORE Someday, not after: confirmed a repeat's currently
            #     "waiting" instance commonly carries destination=SOMEDAY too
            #     (an empty/no-fixed-date waiting instance and a genuine
            #     Someday task apparently share that same destination value),
            #     but a waiting repeat must still surface in Today, not get
            #     silently absorbed into Someday the way a real stale Someday
            #     task should be.
            is_someday = t.destination is Destination.SOMEDAY
            # repeat_is_active (not just "has an rt reference") -- see
            # _collapse_recurring_instances for why the distinction matters.
            is_waiting_repeat = t.repeat_is_active
            if due and due == today:
                today_l.append(t)
            elif sched and sched > today:
                upcoming.append(t)
            elif is_waiting_repeat and (not sched or sched < today):
                today_l.append(t)
            elif is_someday:
                someday.append(t)
            elif sched and sched == today:
                today_l.append(t)
            else:
                anytime.append(t)
        logbook.sort(key=lambda t: (t.completion_date or date.min), reverse=True)
        return SmartLists(inbox, today_l, upcoming, anytime, someday, logbook)

    def search(self, query: str) -> list[Task]:
        q = query.lower().strip()
        if not q:
            return []
        out = []
        for t in self.tasks:
            if (
                q in t.title.lower()
                or q in t.notes.lower()
                or any(q in tag.lower() for tag in t.tags)
            ):
                out.append(t)
        return out

    # ===================================================================== #
    # builders
    # ===================================================================== #
    @classmethod
    def from_entities(cls, entities: dict[str, Entity]) -> "Database":
        """Build the model from replayed Things Cloud entities."""
        # 1. figure out which uuids are areas (referenced by `areas`, or Area* type)
        referenced_areas: set[str] = set()
        tag_refs: set[str] = set()
        # uuids referenced as another task's repeating_template -- a repeat
        # template's own NEW event can be compacted out of the log (confirmed
        # on a real account for two active repeats), leaving it title-less and
        # type-less. Being referenced this way is itself real evidence it's a
        # meaningful entity, same as being a project/area/heading parent.
        referenced_templates: set[str] = set()
        checklist_by_task: dict[str, list[tuple[str, bool, int]]] = {}

        for ent in entities.values():
            if ent.entity_type == P.ENTITY_TASK:
                for aid in ent.fields.get("areas", []) or []:
                    referenced_areas.add(aid)
                for tg in ent.fields.get("tags", []) or []:
                    if isinstance(tg, str):
                        tag_refs.add(tg)
                for rtid in ent.fields.get("repeating_template", []) or []:
                    referenced_templates.add(rtid)

        def title_of(uuid: str) -> str:
            e = entities.get(uuid)
            return (e.fields.get("title") if e else None) or ""

        # 2. checklist items -> attach to parent task
        for ent in entities.values():
            if ent.entity_type == P.ENTITY_CHECKLIST:
                parents = ent.fields.get("tasks") or ent.fields.get("ts") or []
                done = ent.fields.get("status") == P.STATUS_COMPLETE
                text = ent.fields.get("title", "")
                idx = ent.fields.get("index", 0) or 0
                for pid in parents:
                    checklist_by_task.setdefault(pid, []).append((text, done, idx))

        areas: list[Area] = []
        projects: list[Task] = []
        tasks: list[Task] = []
        headings: dict[str, Entity] = {}

        # collect headings first so tasks can resolve their heading title
        for ent in entities.values():
            if ent.entity_type == P.ENTITY_TASK and ent.fields.get("type") == P.TYPE_HEADING:
                headings[ent.uuid] = ent

        for uuid, ent in entities.items():
            etype = ent.entity_type
            is_area = uuid in referenced_areas or etype.startswith("Area")
            if is_area and etype != P.ENTITY_TASK:
                areas.append(
                    Area(uuid=uuid, title=title_of(uuid), index=ent.fields.get("index", 0) or 0)
                )
                continue
            if etype != P.ENTITY_TASK:
                continue  # checklist items handled above; unknown types skipped

            f = ent.fields
            # Skip only TRULY empty fragments: an entity reconstructed from a stray
            # edit with no title, no type, and no parent is noise. Anything with a
            # title or a parent is kept (it's real, just partially known).
            #
            # `type` is the one field checked for bare presence, not "type" OR
            # "status" OR "destination" as this used to read: on a real account,
            # ~35% of Task6 entities turned out to be EDIT-only fragments that were
            # never given a NEW (server-side log compaction, or genesis not
            # captured -- see REVERSE-ENGINEERING.md sections 7/8). Most of those still
            # carry `status`/`destination` (a stray "mark done" or "move to
            # Anytime" EDIT touches those, not `type`), so accepting any one of the
            # three materialized hundreds of untitled, unattached, floating
            # fragments as ordinary open tasks. `status`/`destination` alone are
            # too common on partial edits to mean "this is a real task"; `type`
            # only appears on an entity that was actually created with knowledge
            # of what it is.
            title_present = bool((f.get("title") or "").strip())
            has_type = "type" in f
            has_parent = bool(f.get("projects") or f.get("areas") or f.get("action_group"))
            # A repeat template referenced by a real instance is meaningful
            # evidence too, even title-less and type-less (its own NEW event
            # can be compacted away -- confirmed for two active repeats on a
            # real account; without this, an active chain with no currently
            # open instance has nothing left to stand in as its "waiting"
            # placeholder in Today -- see _collapse_recurring_instances).
            is_referenced_template = uuid in referenced_templates
            if not (title_present or has_type or has_parent or is_referenced_template):
                continue
            tp = f.get("type", P.TYPE_TASK)
            if tp == P.TYPE_HEADING:
                continue  # headings are structural; surfaced via heading_title

            notes_text = _note_text(f.get("note"))
            tags = [title_of(t) or t if isinstance(t, str) else str(t) for t in (f.get("tags") or [])]
            project_ids = f.get("projects") or []
            area_ids = f.get("areas") or []
            heading_ids = f.get("action_group") or []
            heading_id = heading_ids[0] if heading_ids else None
            rt_ids = f.get("repeating_template") or []

            common = dict(
                uuid=uuid,
                title=f.get("title", ""),
                status=_status_from_int(f.get("status", P.STATUS_TODO)),
                # When a task has no explicit destination, default to Anytime, NOT
                # Inbox -- defaulting unknowns to Inbox floods it with stray items.
                destination=_dest_from_int(f.get("destination", P.DEST_ANYTIME)),
                notes=notes_text or "",
                tags=tags,
                deadline=_ts_to_date(f.get("due_date")),
                scheduled=_ts_to_date(f.get("scheduled_date")),
                completion_date=_ts_to_date(f.get("completion_date")),
                is_evening=bool(f.get("evening")),
                trashed=bool(f.get("trashed")),
                index=f.get("index", 0) or 0,
                today_index=f.get("today_index", 0) or 0,
                area_id=area_ids[0] if area_ids else None,
                repeating_template_id=rt_ids[0] if rt_ids else None,
            )

            if tp == P.TYPE_PROJECT:
                projects.append(Task(kind=Kind.PROJECT, **common))
            else:
                cl = sorted(checklist_by_task.get(uuid, []), key=lambda c: c[2])
                tasks.append(
                    Task(
                        kind=Kind.TASK,
                        project_id=project_ids[0] if project_ids else None,
                        heading_id=heading_id,
                        heading_title=title_of(heading_id) if heading_id else "",
                        checklist=[(t, d) for (t, d, _i) in cl],
                        **common,
                    )
                )

        # de-duplicate areas (an area can be referenced many times)
        seen = {}
        for a in areas:
            seen[a.uuid] = a
        areas = sorted(seen.values(), key=lambda a: a.index)
        # Things supports repeating projects too, not just repeating tasks --
        # apply the same collapsing to both lists so a template-project can't
        # show in the sidebar the same way a template-task used to show in
        # every list (no repeating project existed in the account this was
        # built against to confirm against, but the mechanism is identical).
        projects = _collapse_recurring_instances(projects, entities)
        tasks = _collapse_recurring_instances(tasks, entities)
        projects.sort(key=lambda p: p.index)
        tasks.sort(key=lambda t: t.index)

        stamp = datetime.now().strftime("%d %b %Y, %H:%M")
        return cls(areas, projects, tasks, exported_at=stamp)

    @classmethod
    def from_export(cls, data: dict) -> "Database":
        """Build the model from the iOS Shortcuts JSON export (your original format)."""
        def parse_date(s):
            if not s:
                return None
            m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", str(s))
            if not m:
                return None
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y").date()
            except ValueError:
                try:
                    return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y").date()
                except ValueError:
                    return None

        def parse_status(s):
            return {"Completed": Status.COMPLETED, "Canceled": Status.CANCELED}.get(s, Status.OPEN)

        def truthy(v):
            return v in (True, "Yes", "yes", "true", 1, "1")

        def parse_checklist(s):
            out = []
            for line in str(s or "").split("\n"):
                line = line.strip()
                if not line:
                    continue
                done = bool(re.match(r"^-\s*\[[xX]\]", line))
                text = re.sub(r"^-\s*\[[xX ]\]\s*", "", line).strip()
                if text:
                    out.append((text, done))
            return out

        areas = [Area(uuid=a["id"], title=a.get("title", ""), index=i)
                 for i, a in enumerate(data.get("areas", []))]
        projects = []
        for i, p in enumerate(data.get("projects", [])):
            projects.append(Task(
                uuid=p["id"], title=p.get("title", ""), kind=Kind.PROJECT,
                status=parse_status(p.get("status")), notes=p.get("notes", "") or "",
                deadline=parse_date(p.get("deadline")), area_id=p.get("parent_id"),
                index=i,
            ))
        proj_ids = {p.uuid for p in projects}
        tasks = []
        for i, t in enumerate(data.get("todos", [])):
            parent = t.get("parent_id")
            start = t.get("start", "")
            dest = Destination.INBOX if truthy(t.get("is_inbox")) else (
                Destination.SOMEDAY if start == "Someday" else Destination.ANYTIME)
            tasks.append(Task(
                uuid=t["id"], title=t.get("title", ""), kind=Kind.TASK,
                status=parse_status(t.get("status")),
                destination=dest,
                notes=t.get("notes", "") or "",
                tags=[s.strip() for s in str(t.get("tags", "")).split(",") if s.strip()],
                deadline=parse_date(t.get("deadline")),
                scheduled=parse_date(t.get("start_date")) if start == "On Date" else None,
                heading_title=(t.get("heading") or "").strip(),
                checklist=parse_checklist(t.get("checklist")),
                project_id=parent if parent in proj_ids else None,
                area_id=parent if parent not in proj_ids else None,
                index=i,
            ))
        return cls(areas, projects, tasks, exported_at=data.get("exported_at", ""))


def parse_export_text(raw: str) -> dict:
    """The Shortcut embeds literal newlines inside JSON strings (invalid JSON)."""
    import json

    return json.loads(raw.replace("\n", "\\n"))
