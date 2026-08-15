# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A native GTK4 / libadwaita desktop app (Python) that reads (and, for one operation, writes) Things 3 tasks on Linux. It talks directly to Things Cloud using a reverse-engineered implementation of the same sync protocol the official Things Mac client uses. It can also import the JSON an iOS Shortcut export produces, so it works without signing in to the cloud at all.

This is an **unofficial** interop client, not affiliated with Cultured Code. The protocol is undocumented and can change at any time — see `REVERSE-ENGINEERING.md` for the full protocol write-up and how to extend it (including the guarded path to adding writes).

## Running

```bash
# Runtime deps (system packages — GObject-introspection needs the distro build)
sudo dnf install python3-gobject gtk4 libadwaita python3-requests python3-keyring

python3 run.py                 # run from source
pip install --user .           # or install as `things-gnome` + desktop entry
```

PyGObject is intentionally **not** a pip dependency — it must come from the system package (`python3-gobject`) so it matches the installed GTK. `pip install .` only pulls `requests` and `keyring`.

## Tests

```bash
python3 tests/test_replay.py             # replay + model + smart lists + export import (no network, no display)
python3 tests/test_pagination.py         # pagination/replay regression tests (no network)
python3 tests/test_commit.py             # write-delta shapes, date round-trip, dry-run/live commit client (no network)
xvfb-run -a python3 tests/test_ui_smoke.py   # builds and renders every view headlessly under Xvfb
```

Each test file is also directly executable and self-contained (`sys.path` is patched at the top of each), so a single file can be run standalone during iteration — there's no pytest config/fixtures to route through.

`tools/diagnose.py` signs in and dumps *redacted* structural statistics about a real account's history log (no titles/notes/tags ever printed) — useful when the cloud protocol appears to have changed. `tools/capture.py` mechanizes the "make one change in the real app, diff the tail of the log" loop for specific fields that are still unconfirmed (see `REVERSE-ENGINEERING.md` §8) — same redaction, plus it saves a labeled JSON log of each capture.

## Architecture

The codebase is layered so the wire format never leaks past the sync layer:

```
sync/protocol.py   wire constants + two-letter field map (FIELD) + decode_fields()
sync/auth.py        login() -> Account (history_key, head_index)
sync/client.py       ThingsReadClient: paginated fetch + NEW/EDIT/t=2 replay -> Entity map
sync/commit.py       ThingsWriteClient: EDIT/NEW commits to the history log, dry-run by default
model.py             Entity map -> Database (Area/Task, smart-list rules) — the only consumer of raw entities;
                      also the write-delta builders (Task.complete_delta() etc.)
store.py             Credentials (keyring, XDG) + Cache (replayed entities, versioned)
application.py       Adw.Application entry point
window.py            entire UI: NavigationSplitView, sync threading, login/preferences dialogs, checkbox write path
```

**The sync protocol** (`sync/protocol.py`'s module docstring has the full explanation): Things Cloud data is an **append-only event log**, not a snapshot. Reading means paging `GET .../history/{history_key}/items?start-index=N` and replaying each item:
- `t==0` (NEW): payload is the full object — create it.
- `t==1` (EDIT): payload is a partial delta — merge onto existing state.
- `t==2` (BASELINE_OR_DELETE): a state baseline when it carries a payload; **a deletion when empty** — synthesizes `trashed=True` (see `sync/client.py`'s `_apply`). This flipped twice: originally treated as a no-op (an earlier "empty = delete" guess was reverted for risking data loss), then confirmed correct on a real account (every `t=2` observed, 256/256, was empty, and cross-referencing specific uuids showed real tasks the user had deleted elsewhere still shown as active). See `REVERSE-ENGINEERING.md` §6.

**Pagination is the historical footgun here**: `current-item-index` in the response is the log's HEAD (total length), not the next offset — it stays ~constant across pages. You must advance `start-index` by the number of items each page actually returned and keep paging until a page comes back empty. Advancing straight to `current-item-index` silently truncates the sync to the first batch. `tests/test_pagination.py` is a regression test for exactly this bug (a title-setting EDIT and a tail-of-log item that only appear in a later batch).

**`model.py`** turns the replayed `Entity` map into `Area`/`Task` domain objects via `Database.from_entities()`, or builds the same model from a Shortcuts JSON export via `Database.from_export()` — both normalize to the same `Database`/`Task` shape so the rest of the app doesn't care which source it got.

**`Database.categorize()`** started as a straight port of the smart-list rules from the original `things-viewer.html`, but that port's core assumption — "Today = `scheduled_date <= today`" — turned out to be wrong, confirmed by directly diffing this app's output against a real account's real phone state. The actual, confirmed rules (all documented in the function's own comments, and in depth in `REVERSE-ENGINEERING.md` §9):
- **Today is `scheduled_date == today` exactly** (or `deadline == today`, or a waiting repeat instance — see below) — not "any date on or before today". A years-stale scheduled task just sits quietly in Anytime; Things does not keep re-surfacing it in Today forever.
- **A Someday task given a future date is Upcoming, not Someday** — picking a date "graduates" it out of Someday for display, even though `destination` stays `SOMEDAY`. Only a *stale past* date leaves it in Someday.
- **Recurring tasks need `Task.repeat_is_active`, not just a `repeating_template_id`.** Not every `rt` reference is a live repeat (some are one-off tasks pointing at a template that was defined but never actually run — `instance_creation_count == 0`). An active chain's "waiting" instance shows in Today regardless of staleness or its own `destination`; a genuinely active chain can even have *zero* open instances anywhere in the log, in which case the template entity itself becomes the "waiting" placeholder (borrowing a title from an instance if its own was lost to compaction). See `_collapse_recurring_instances` / `_select_current_recurring_instance`.
- Some stale-looking data is a **permanent, unrecoverable gap** in what Things Cloud's own history log retains (confirmed: a task completed years ago with zero trace of that completion anywhere in the readable log) — not a bug to keep chasing. `REVERSE-ENGINEERING.md` §9.6 has the detail.

**Caching**: `store.Cache` persists the replayed entity map plus a `REPLAY_VERSION` int. Bump `REPLAY_VERSION` whenever replay/model logic changes in a way that makes an existing cache stale — a mismatch forces a full re-sync from index 0 automatically, so logic fixes take effect without asking users to manually resync.

**Credentials**: email goes in a plain settings file; the password goes into the system keyring via the `keyring` package, falling back to a `0600` file (and telling the user it did) if no keyring backend is available.

**UI**: `window.py` is a single `Adw.ApplicationWindow` — `Adw.NavigationSplitView` sidebar (smart lists + areas/projects) driving a content pane. Sync runs on a background `threading.Thread`; results come back to the GTK main loop via `GLib.idle_add`. All row titles/subtitles go through `esc()` (`GLib.markup_escape_text`) before hitting Adw widgets, since they parse Pango markup by default and unescaped `&`/`<`/`>` (e.g. in a URL) breaks rendering.

## Writing is opt-in and minimal by design

Writing means committing new events against the current head index in an account with no undo — getting a field wrong, or racing a real device mid-edit, risks corrupting live data. Only one write operation exists: completing/uncompleting a task (`Task.complete_delta()`/`uncomplete_delta()`/`cancel_delta()` in `model.py`, `ThingsWriteClient` in `sync/commit.py`, wired to the task-row checkbox in `window.py`). It's gated behind **two** switches in Preferences — "Enable writing (experimental)" (off by default) and a "Dry run" sub-switch (on by default) — so enabling writing alone still sends nothing.

Three things make that write path safe, and matter if you extend it:
- **Never filter `None` out of a delta.** Explicit `null` is how a field gets cleared server-side (e.g. `sp: null` on uncomplete); a generic "drop empty fields" serializer structurally cannot clear anything. Delta builders emit literal two-letter dicts by hand for this reason, not through a generic encoder.
- **Guard every checkbox rebuild.** `window.py`'s `_render_content` sets `self._rendering = True` for the duration of the rebuild; the toggle handler bails out immediately if that flag is set. Without it, re-rendering a Logbook view with 200 completed tasks would fire 200 commits the moment writes go live — invisible in dry-run (just extra log lines), catastrophic live.
- **Apply your own commit locally, then adopt `server-head-index` — never re-sync from it.** The commit you just made *is* what advanced the head to that index; a fresh read starting there returns an empty page and the UI appears to silently revert.

`REVERSE-ENGINEERING.md` §4 has the full commit mechanism and remaining implementation order; §6 documents what's cross-confirmed against three community reference implementations (including a full NEW-task payload, confirmed recurrence-rule structure, and Tag4/Area3/ChecklistItem3 wire shapes); §8 lists the specific live captures (via `tools/capture.py`) still needed before extending further; §9 has the full smart-list categorization findings (Today/Upcoming/Someday/recurring-task rules) confirmed against a real account and phone.

## Protocol field map

`sync/protocol.py`'s `FIELD` dict is the single source of truth mapping the API's cryptic two-letter keys (`tt`, `ss`, `st`, `sr`, `dd`, `ar`, `pr`, `agr`, …) to human names. When decoding new fields, add them there rather than hardcoding two-letter keys elsewhere — `decode_fields()` preserves unknown keys under their original name so nothing is silently dropped while reverse-engineering.
