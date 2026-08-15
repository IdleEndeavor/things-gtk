# Reverse-engineering Things Cloud, and extending this app

You asked how to push this past the MVP yourself. This is the working guide. It
documents everything the app already relies on, then shows you how to discover the
parts it doesn't, and how to add real functionality (completing tasks, creating
tasks, scheduling, reminders, repeats) **safely**.

Everything here is unofficial. Things Cloud is undocumented; Cultured Code can
change it whenever they like. Treat your live account as production data with no
undo.

---

## 1. The shape of the system

There is essentially one big idea to hold in your head:

**Your Things data is not a document you GET and PUT. It is an append-only event
log.** Every change any device makes — create a task, tick it off, rename a
project — is appended as one *item* to a per-account "history". To know the
current state of your tasks, you fetch the log from the beginning and **replay**
it. To change something, you **append** a new item.

Once that clicks, the whole protocol is small.

### 1.1 Endpoints

```
Base:    https://cloud.culturedcode.com/version/1

# A. Resolve the account (gives you the history-key — the handle to all your data)
GET  /account/{url-encoded-email}
     Authorization: Password {url-encoded-password}
  -> { "history-key": "<uuid>", "maildrop-email": "...", ... }

# B. Open a session (gives the current head index + a session secret; needed for writes)
POST https://cloud.culturedcode.com/api/account/login/getT3SharedSession
     Authorization: B64SON {base64( json({"ep":{"e":<email>,"p":<password>}}) )}
  -> { "headIndex": <int>, "historyKeySessionSecret": "...", ... }

# C. Read the log (paginated)
GET  /history/{history_key}/items?start-index={offset}
  -> { "current-item-index": <int>, "items": [ {<uuid>: {"t":0|1,"e":"Task6","p":{...}}}, ... ] }

# D. Append to the log (this is a WRITE — not used by the MVP)
POST /history/{history_key}/commit?ancestor-index={offset}&_cnt=1
     body: { "<uuid>": {"t":0|1, "e":"Task6", "p":{...}} }
```

### 1.2 Headers

The Mac client sends these; the app re-sends them (see `sync/protocol.py`):

```
Schema: 301
App-Id: com.culturedcode.ThingsMac
App-Instance-Id: -com.culturedcode.ThingsMac
Content-Type: application/json; charset=UTF-8
User-Agent: <Mac client UA>
```

Reads don't appear to validate the User-Agent strictly. If a future server build
starts to, capture the real UA (next section) and set it in **Preferences →
User-Agent override**, or via the `THINGS_USER_AGENT` environment variable.

### 1.3 Replaying the log

```
state = {}                      # uuid -> entity
for item in every item, in order:
    uuid, body = the single key/value
    if body["t"] == 0:          # NEW  — p is the full object
        state[uuid] = decode(body["p"])
    elif body["t"] == 1:        # EDIT — p is a partial delta
        state[uuid].update(decode(body["p"]))
```

Keep requesting, **advancing `start-index` by the number of items each page
returned**, until a page comes back empty. `current-item-index` is the log's
*head* (its total length) and stays roughly constant across pages — it is **not**
the next offset, so don't set `start-index` to it (that consumes only the first
batch and leaves you with a stale snapshot). That's exactly what
`sync/client.py: ThingsReadClient.replay()` does, and the incremental case
(passing the last head back in as `start-index`) is how you sync cheaply
afterwards.

---

## 2. Capturing live traffic (how to discover the rest yourself)

The field map in `sync/protocol.py` is good but not complete. When you want to
know how Things encodes something the app doesn't handle yet — a repeating rule, a
reminder, a particular date edge case — the reliable method is to **watch the real
client do it** and diff the log.

### 2.1 Set up an intercepting proxy

`mitmproxy` is the friendliest (`sudo dnf install mitmproxy`). The catch is TLS
pinning: depending on the Things build, the Mac/iOS app may pin its certificate,
in which case a vanilla proxy CA won't be trusted.

Easiest, most reliable path: **drive the protocol yourself** rather than sniff the
official app. You already have valid credentials and the endpoints above, so you
can:

1. Resolve your account (A) and read your head index (B).
2. In the **real** Things app, make one specific change (e.g. set a deadline on a
   throwaway task).
3. Wait for it to sync, then fetch the log from your previously-noted index (C).
4. The new items are exactly the encoding of that one change. Diff them against
   what you expected.

This "make one change, read the tail of the log" loop is the single most useful
technique in this whole document. It needs no proxy and no certificate work, and
it shows you the *exact* bytes the official client commits — which is what you'd
want to imitate for writes anyway.

If you do want to sniff the app directly (handy for the login/session calls and
headers): run mitmproxy, trust its CA in your OS trust store, point the machine's
proxy at it, and try the **Mac** app first — desktop builds pin less aggressively
than iOS. On iOS you'll likely need a jailbroken device or a build with pinning
disabled, which is usually more trouble than the log-diff loop above.

### 2.2 A scratch script for the loop

`sync/client.py` already gives you everything; a few lines drive it:

```python
from thingsgnome.sync import login, ThingsReadClient

acc = login("you@example.com", "password")
client = ThingsReadClient(acc)

before = client.replay()                      # full state + head index
print("head:", before.head_index)
input("Make ONE change in Things, let it sync, then press Enter…")
after = client.replay(start_index=before.head_index, entities=before.entities)
# after.entities now contains only-the-changed items merged in;
# inspect the raw tail instead if you want the literal NEW/EDIT payloads.
```

To see the *raw* items (before decoding), call the lower-level fetch in
`client.py` and print the JSON straight from `items` — that's the ground truth for
two-letter keys you haven't mapped yet.

---

## 3. Fields still worth decoding

These keys appear in real payloads and are carried through `decode_fields()` but
not yet interpreted by the model. Good first targets:

- `rr` **recurrence_rule** — a JSON object describing repeats, not XML (see §6 for
  the confirmed field-by-field structure, cross-checked against two independent
  community implementations). The one piece still fuzzy is `of` (the actual
  weekday/ordinal selector) — make a daily, then a "every 2 weeks on Tue/Thu",
  then a "monthly on the last day" repeat and diff the `rr.of` values (§8).
- `rp` **repeater** / `rt` **repeating_template** / `rmd` **repeater_migration_date**
  / `icsd` `instance_creation_start_date` / `acrd` — the machinery around repeating
  task *instances*. Repeats are the most complex corner; map `rr` first.
- `ato` **reminder** — seconds since midnight for a time-of-day alarm; pair it with
  `sr` (scheduled date) to render "Today 09:00".
- `dl` **delegate**, `do` **due_date_offset**, `dds` **due_date_suppression_date**
  — deadline-related niceties.
- `xx` — opaque blob (`{"sn":{},"_t":"oo"}`); leave it alone unless a diff shows it
  changing meaningfully.
- **Tags as entities.** `tg` is sometimes `[uuid]` and sometimes `[string]`.
  There's a tag entity in the log (alongside `Task6`/`ChecklistItem3`); capture its
  entity name from a fresh tag creation and build a `uuid -> tag name` map so tag
  chips always show names. The model already tolerates both forms.
- **Areas.** The app detects areas structurally (any uuid referenced by a task's
  `ar`, or an entity whose type starts with `Area`). If you want area *titles* and
  ordering exactly right, capture the area entity name the same way.

When you decode a new key, add it to `FIELD` in `protocol.py` and, if the UI should
show it, surface it on the `Task`/`Area` dataclass in `model.py`.

---

## 4. Adding writes (the safe path to Mac-app parity)

This is where it gets powerful and where you can do real damage. Build it
incrementally and test against a **throwaway account or a throwaway area** first,
never your live data.

> **Status: step 1 of §4.2 is implemented** — `sync/commit.py`, the delta
> methods on `Task` in `model.py`, and the checkbox wiring in `window.py`,
> gated behind Preferences → Enable writing (off by default) and a Dry run
> sub-switch (on by default). See §6 for what this was cross-checked against
> and §8 for what to capture before extending it further.

### 4.1 The mechanism

A write is a `commit` (endpoint D). You need:

- the **history_key** (you have it from login),
- the current **head index** as `ancestor-index` (from a fresh read, or the session
  call B — always read immediately before committing so you're not stale),
- a body of `{uuid: {"t":0|1, "e":"Task6", "p":{…}}}`:
  - `t:1` **EDIT** an existing item — `uuid` is the existing task's id, `p` is only
    the changed fields. **Start here**; it's the safest.
  - `t:0` **NEW** item — `uuid` is a fresh 22-char id you generate, `p` is the full
    object.

After a successful commit the server advances the head index; re-read to confirm
your change replays back the way you expect.

### 4.2 Suggested order of implementation

1. **✅ Complete / uncomplete a task — implemented.** EDIT with `ss` =
   `STATUS_COMPLETE` (3) and `sp` (completion_date) set to the same timestamp as
   `md`; uncomplete = `ss` `STATUS_TODO` (0), `sp` explicit `null`. See
   `Task.complete_delta()` / `uncomplete_delta()` / `cancel_delta()` in
   `model.py` and `tests/test_commit.py`.
2. **Edit a title / note.** EDIT `tt`, or `nt` (`{"_t":"tx","v":<text>}`).
3. **Schedule ("when").** EDIT `sr` (int unix ts) for a date; clear it for Anytime;
   set the evening bit `sb` for "this evening".
4. **Set / clear a deadline.** EDIT `dd`.
5. **Create a task in Inbox.** NEW `Task6` with `tt`, `tp:0`, `ss:0`, `st:0`
   (inbox), `cd`/`md` = now. Then learn `ix`/`ti` ordering.
6. **Move between lists / into a project or area.** EDIT `pr` / `ar` / `agr` and the
   destination `st`.
7. **Reminders, then repeats.** `ato`, then the `rr` family — last, because repeats
   are the fiddliest.

### 4.3 Guardrails to build in

- A **dry-run mode** that logs the exact commit body without sending it. Diff it
  against what the real Mac app commits for the same action (section 2.1) before you
  trust it.
- Always **read head index immediately before commit**; abort if it moved
  unexpectedly between your read and your write.
- Keep a **local backup** of the full replayed state before your first writes
  (the app already caches it under your XDG data dir — copy that file).
- Gate it all behind **Preferences → "Enable writing (experimental)"** (off by
  default) plus a **Dry run** sub-switch (on by default, so enabling writing
  alone still sends nothing) — both wired in `window.py`, persisted via
  `Credentials.save_settings()` as `writes_enabled` / `dry_run`.
- Rebuild rows behind a `self._rendering` guard, and connect a checkbox's
  `toggled` handler only *after* setting its initial `active` state. A
  re-render that constructs N completed-task checkboxes and connects a handler
  before setting `active` (or without the guard) will fire N unwanted commits
  the moment writes go live — this is easy to get wrong and easy to miss in
  dry-run, since it just logs N lines instead of visibly breaking anything.
- Serialize commits (a single-flight lock, e.g. `self.committing`) — Things
  Cloud has no optimistic merge, so two in-flight commits racing the same
  `ancestor-index` is a real risk, not just a UI nicety.
- After a successful commit, **apply your own delta locally and adopt the
  returned `server-head-index`** — do not re-sync starting from that new head.
  The commit you just made *is* what advanced the head to that index, so a
  fresh read starting there will return an empty page and the UI will appear
  to silently revert your change.

### 4.4 Where the code goes

- `sync/commit.py`: `ThingsWriteClient` mirrors the read client — `edit()`
  builds the `{uuid:{t,e,p}}` body, POST to `commit`, parse
  `server-head-index` from the response. `dry_run=True` (the default) never
  touches the network. There's deliberately no `new()` yet — a `t:0` NEW
  commit needs the full ~34-field object from §6, and that shape has an
  unresolved disagreement between references (the `nt` default); add it once
  §8 item 7 resolves that, backed by a payload builder and a test the same way
  `edit()` is backed by `Task.complete_delta()`.
- `model.py`: `Task.complete_delta()` / `uncomplete_delta()` / `cancel_delta()`
  build the *encoded* (two-letter) partial payloads as literal dicts — not
  through a generic encoder — so a typo'd field name is visible at the call
  site instead of silently passing through.
- `window.py`: the task-row checkbox is sensitive only when writes are
  enabled; toggling it runs the commit on a background thread (same pattern as
  `_sync_worker`), applies the delta locally, and re-renders from the adopted
  head — see `_on_task_toggled` / `_commit_worker` / `_commit_done`.

---

## 5. Going further toward Mac-app parity

Once writes work, the remaining gap to the Mac app is mostly UI, not protocol:

- Drag-to-reorder (you'll be writing `ix` / `ti`).
- Quick Entry / global new-task shortcut.
- Today's evening section, the Upcoming calendar strip, repeating-task instances.
- Trash view and restore (`tr` flag) and proper cancel (`ss` = 2).
- Multi-select and bulk edits (several EDITs in one commit body — note `_cnt`).

None of these need new endpoints — just more of the field map and more UI. The
protocol you already have is the whole surface.

---

## 6. Cross-confirmed against community write implementations

The write path (§4) is no longer just a plan — `sync/commit.py` (`ThingsWriteClient`)
and `Task.complete_delta()` / `uncomplete_delta()` / `cancel_delta()` in `model.py`
implement step 1 of §4.2 ("complete / uncomplete a task"), wired into the task-row
checkbox in `window.py` behind **Preferences → Enable writing (experimental)**
(default off) and a **Dry run** sub-switch (default on, so enabling writing alone
does not start sending anything). `tests/test_commit.py` covers the delta shapes,
the date round-trip, and that dry-run never touches the network.

This was cross-checked against three community projects beyond the two already
cited in the README, which between them have live-tested write support (one has a
`live-cloud-test.yml` CI workflow that runs against a real account):

- **[disrupted/things-cloud-api](https://github.com/disrupted/things-cloud-api)**
  (Python) — full `commit` mechanics, the exact full-object shape a `t:0` NEW
  task needs (every field present, not a sparse dict), and project/area
  assignment rules (assigning a project clears area and vice versa, and pulls
  the task out of Inbox).
- **[evanpurkhiser/things3-cloud](https://github.com/evanpurkhiser/things3-cloud)**
  (Rust) — the most complete reference found. Its `trycmd/` fixtures are
  recorded request bodies from real command runs, i.e. as close to ground truth
  as this document gets without your own capture. Confirms recurrence rule
  fields, tag/area/checklist wire shapes, and the ID-generation algorithm.
- **[wbopan/things-cloud-mcp](https://github.com/wbopan/things-cloud-mcp)** (Go)
  — an independent SDK with its own recurrence handling and a design doc for
  recurring tasks; useful as a second opinion on §6's `rr` structure.
- (Already cited in the README:) **nicolai86/things-cloud-sdk** (Go).

### Confirmed: full NEW task payload

From `evanpurkhiser/things3-cloud`'s `bare_create.trycmd` (a recorded, tested
command run) — every field present, defaulted, nothing sparse:

```json
{
  "acrd": null, "agr": [], "ar": [], "ato": null,
  "cd": 1700000000.0, "dd": null, "dds": null, "dl": [], "do": 0,
  "icc": 0, "icp": false, "icsd": null, "ix": 0, "lai": null, "lt": false,
  "md": 1700000000.0, "nt": null, "pr": [], "rmd": null, "rp": null,
  "rr": null, "rt": [], "sb": 0, "sp": null, "sr": null, "ss": 0, "st": 0,
  "tg": [], "ti": 0, "tir": null, "tp": 0, "tr": false, "tt": "Ship release",
  "xx": {"_t": "oo", "sn": {}}
}
```

Note `"nt": null` for an empty note — `disrupted/things-cloud-api` instead
defaults to `{"_t":"tx","ch":0,"v":"","t":1}`. The two references disagree here;
neither is confirmed against this codebase's own live account. Capture a fresh
task creation yourself (see §8) before relying on either default.

### Confirmed: mark done / undo (what's now implemented)

From the same project's `mark.rs` unit tests (asserted against literal JSON, not
just described):

```json
done:       {"ss": 3, "sp": <now>, "md": <now>}
incomplete: {"ss": 0, "sp": null,  "md": <now>}
canceled:   {"ss": 2, "sp": <now>, "md": <now>}
```

`sp` (completion_date) is sent as the **same float timestamp as `md`**, not a
separately rounded int as this doc previously assumed — that's what
`Task.complete_delta()` now does. `sp: null` on undo must be an explicit key,
never simply omitted (a naive "drop empty fields" serializer — which is what
`disrupted/things-cloud-api`'s own `EditBody.to_api_payload()` does via
`exclude_none=True` — structurally cannot clear a field; its own `todo()`
method can't send this). `model.py`'s deltas are hand-built dicts specifically
to avoid that trap.

### Confirmed: scheduling and deadline (not yet implemented here)

```json
schedule --when today:
  {"md": <now>, "sb": 0, "sr": <midnight-today-utc>, "st": 1, "tir": <midnight-today-utc>}

schedule --deadline <date>:
  {"dd": <midnight-of-date-utc>, "md": <now>}
```

New information: scheduling for Today also sets `tir`
(`today_index_reference_date`) to the same day as `sr` — previously this field
was in the map but its purpose unconfirmed. Implement `Task.schedule_delta()` /
`Task.deadline_delta()` following the pattern in `Task.complete_delta()` when
you pick this phase up, and set `tir` alongside `sr`.

### Confirmed: recurrence rule (`rr`) structure

Both `evanpurkhiser/things3-cloud`'s `src/wire/recurrence.rs` and
`wbopan/things-cloud-mcp`'s Go `repeat.go` independently model `rr` as a JSON
object (not XML), matching what §7 already inferred from a live account:

```
tp   repeat_type      : 0 fixed-schedule, 1 after-completion
fu   frequency_unit   : bitmask, see corrected table below
fa   frequency_amount : every N units (e.g. 2 for "every 2 weeks")
of   offsets          : [{...}]  weekday/day/ordinal selectors, shape still fuzzy
sr   start_reference  : timestamp
ia   initial_anchor   : timestamp
ed   end_date         : timestamp, default ~64092211200 (effectively never)
rc   repeat_count     : int
ts   task_skip        : int, meaning unclear
rrv  recurrence_rule_version : currently 4
```

**`fu`'s values above were wrong** (community-sourced, untested against a real
account) — corrected and confirmed here against a live account, three
independent ways at once:

```
fu   4   year    (e.g. of: {"dy":14,"mo":1} — a fixed day-of-year anniversary)
fu   8   month
fu  16   day
fu 256   week    (paired with of: {"wd": <0-6>} — a fixed weekday)
```

These are exactly the **legacy, deprecated** `NSCalendarUnit` bitmask values
from Foundation (`NSYearCalendarUnit = 1<<2 = 4`, `NSMonthCalendarUnit =
1<<3 = 8`, `NSDayCalendarUnit = 1<<4 = 16`, `NSWeekCalendarUnit = 1<<8 = 256`
— note this is the old single "week" unit, not the modern
`weekOfYear`/`weekOfMonth` split at different bit positions). Things is
almost certainly serializing an old `NSCalendarUnit` bitmask directly rather
than inventing its own enum — plausible for a codebase this age — which is
worth knowing if another `fu` value turns up later (check the *legacy*
Foundation table, not the modern `Calendar.Component` one, before guessing).
Consistent with this: the one `fu:256` template seen carries `of: {"wd": 4}`
— a weekday selector paired with the "week" unit, the one `of` shape this
doc can partially explain.

Confirmed three ways on a real account, zero counterexamples once you account
for which templates actually retain `recurrence_rule` (see §9.7 — most don't):
- A task literally titled "Create and sort the budget", whose real Things note
  says "Repeats **1 month** after completion", has `fu:8, fa:1, tp:1`.
- A task following the same "Repeats **3 months** after completion" pattern
  ("Get a Haircut") has `fu:8, fa:3`.
- "Change the pillowcase" ("Repeats **7 days** after the previous to-do has
  been completed") has `fu:16, fa:7`, and its last completion
  (`completion_date` = 2026-07-30) plus 7 days lands on exactly the date
  (2026-08-06) the real Upcoming list showed for it — see §9.7.

`of` (the actual weekday/ordinal encoding within a period) is still the
fuzziest piece and exactly what a wrong guess would silently corrupt (a
"repeat" that fires on the wrong days) — still the single highest-value
capture in §8. `tp`/`fu`/`fa` are now solid enough to build on; `of` is not.
**Deliberately still unimplemented**, not an oversight: every `tp:0`
fixed-schedule repeat found on a real account had its `recurrence_rule`
compacted away entirely (`of` is only meaningful for `tp:0` — confirmed it's
vestigial UI-picker state on `tp:1` chains, uncorrelated with their actual
computed dates), so there is no live example on this account to check a
weekday/day-of-month guess against at all. `tp`/`fu`/`fa` alone are what
`_repeat_rule_text` and the after-completion date computation in
`_collapse_recurring_instances` are built on (§9.7) — see below.

### Implemented: human-readable repeat text (`Task.repeat_rule`)

Built from a template's `recurrence_rule` (`tp`/`fu`/`fa` only, never `of` —
see above) in `_repeat_rule_text` (`model.py`), attached to the surviving
instance of a genuinely active chain in `_collapse_recurring_instances`, and
rendered in `window.py`'s task-row `meta` line alongside the existing 📅/⚑
markers. Display-only, like the date-computation fix in §9.7 — never written
back.

Real coverage on one account, checked directly against every repeating task
named in `Full State.md`: **3 of 6** still had a retrievable `recurrence_rule`
on their template (`Get a Haircut` → `fu:8 fa:3` → "Repeats 3 months after
completion"; `Create and sort the budget` → `fu:8 fa:1` → "Repeats 1 month
after completion"; `Change the pillowcase` → `fu:16 fa:7` → "Repeats 7 days
after completion") — all three render exactly the literal phrasing in
`Full State.md`. The other 3 (`Scrub Day`, `Take Body Measurements`, `Tech
Day`) render no repeat line, for two separate reasons, not one: `Scrub Day`
and `Take Body Measurements` have a fully compacted template (same pattern as
§9.7's Tech Day case — no `rr` to build from at all), while a chain can also
render nothing simply because it's **dormant** (`repeat_is_active` false, per
§9.4) even when its template's `rr` did survive — confirmed several such
dormant-but-`rr`-intact templates exist on this account (e.g. `icc:0, fu:8,
fa:2`). That gating is deliberate, matching the rest of the codebase's
"genuinely repeating" semantic, not a second data-loss case. A repeat line
that appears on half a real account's repeating tasks is the actual shape of
this feature; don't expect full coverage.

Units are rendered **literally, not normalized** — `fu:16, fa:7` renders "7
days", not "1 week". That's the only thing the stored data actually
supports, since `fu` simply records whichever unit the rule was created
with. **Suggestive but not independently confirmed** that the real Things
apps do the same: `Full State.md` describes `Change the pillowcase` as "7
days" and `Scrub Day` as "1 week", which would fit — but that dump is a
hand-typed paraphrase (it contains a typo, "afteer"), not literal app copy
seen in a screenshot, and `Scrub Day`'s own template is compacted, so its
`fu` can't be checked directly either way. Treat "no normalization" as this
implementation's own reasonable choice given what the wire format supports,
not a reverse-engineered fact the way `fu`'s bitmask values are.

### Confirmed: Tag4 / Area3 / ChecklistItem3 wire shapes

```
Tag4    tt title · sh keyboard shortcut · ix sort index ·
        pn [parent tag uuids] (tags can nest) · xx conflict metadata
Area3   tt title · tg [tag uuids] (areas can carry tags directly) ·
        ix sort index · xx conflict metadata
ChecklistItem3 (NEW, from a recorded add-checklist-item run):
        {"tt": <title>, "ss": 0, "ts": [<parent task uuid>], "ix": <int>,
         "cd": <now>, "md": <now>}
```

This resolves the "tags as entities" and "areas" TODOs in §3 structurally, but
none of it is wired into `model.py`/`window.py` yet — tag/area *titles* still
come from the structural inference `from_entities()` already does. Wiring a
real `uuid -> Tag4/Area3` lookup is a good next increment.

### Confirmed: ID generation

`evanpurkhiser/things3-cloud`'s `ThingsId`: take a random UUID's canonical
uppercase string form, SHA1-hash it, take the first 16 bytes, base58-encode
with alphabet `123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz`
(the Bitcoin alphabet — excludes `0`, `O`, `I`, `l`), producing a ≤22-char
compact ID. `disrupted/things-cloud-api` instead just used a `shortuuid`
random string, which is a different scheme. Neither is confirmed against a
live commit from this codebase — the server may not care which 22-char scheme
you use, but you won't know until you try a real NEW commit and see if it
sticks. Worth a capture (§8) before this codebase adds task creation.

### Confirmed: reordering uses gapped indices

From `reorder.rs`'s recorded fixture: moving one task can produce **two**
separate commits (each its own `ancestor_index`), one setting the moved
task's `ix` to a value like `2048` and a second *rebalancing a neighbor* to
`3072` — indices are kept ~1024 apart so most inserts don't need to touch
every sibling. Not implemented here; if you build drag-reorder, budget for the
rebalance case, not just "renumber the one you moved."

### ✅ Resolved: empty `t:2` IS a deletion (confirmed on a real account)

This was flagged as an unresolved, do-not-guess conflict: this doc's §7 used to
claim, from an earlier live observation, that empty `t:2` is *not* a reliable
deletion signal (a prior version of this client treated it as one and that
risked dropping live data), while `evanpurkhiser/things3-cloud`'s `delete.rs`
sends the opposite — empty `t:2` *as* the deletion itself.

It's now confirmed directly, not guessed: on a real account, **every** `t:2`
event in the entire log (256 of them, zero exceptions) had an empty payload,
and cross-referencing specific uuids against that account's own recent history
showed ordinary, titled, non-trashed tasks (`Pick up milk`, `Clean my room`,
`Delete Facebook account` — the kind of thing you complete and move on from)
that were deleted on another device and were still showing as fully active
here. A full re-replay with empty-`t:2` treated as a deletion correctly
hid 161 previously-stale titled tasks and matched what the user expected.

`sync/client.py`'s `_apply()` now synthesizes `trashed=True` on an empty `t:2`
(reusing the existing trashed-based filtering everywhere, rather than adding a
new state or discarding the entity) — see the comment there. This resolves the
conflict for the **read** side. It does not yet tell you what a delete
**write** should send — that's still worth a capture (§8 item 1) if you want
to confirm it matches `evanpurkhiser/things3-cloud`'s `{"t":2,"e":"Task6","p":{}}`
before implementing a delete/trash write action, since a write mistake here has
the same no-undo risk this section originally warned about.

---

## 7. Confirmed from a live account

Things observed by dumping a real history log (see `tools/diagnose.py`), so these
are facts rather than guesses:

- **Three update kinds, not two.** Alongside `t:0` NEW and `t:1` EDIT there is
  `t:2`. When it carries a payload it is a **state baseline** (a full-ish snapshot
  of an entity, e.g. from server-side compaction) and must be applied like a
  create-or-merge. When its payload is empty, **it's a deletion** (§6) —
  synthesize `trashed=True` rather than discarding the entity or no-op'ing it.
- **Deletion is via the `tr` (trashed) flag, or an empty-payload `t:2`, or a
  `Tombstone2` entity** — all three have been observed. Filter `tr == true` out
  of every view (this now covers both `tr:true` EDITs and empty `t:2`, since
  `_apply()` synthesizes `trashed` either way); treat a `Tombstone2`-typed uuid
  as dead too. See §6 for how the empty-`t:2` case was confirmed, and its note
  on the write side still being unconfirmed.
- **Entity types seen:** `Task6`, `ChecklistItem3`, `Area3`, `Tag4`, `Settings5`,
  `Tombstone2`. Areas really are `Area3`; tags are `Tag4` (resolve `tg` uuids
  against them for names).
- **The log does not always start at account genesis.** Response metadata includes
  `start-total-content-size` / `end-total-content-size` / `latest-total-content-size`
  and the log can be compacted, so some uuids appear only via EDIT/baseline events
  with no NEW. Reconstruct what you can; don't invent missing state.
- **Tasks are usually created with an empty title**, then titled by a later EDIT —
  so empty-title NEW events are normal; you must apply the EDITs to get titles.
- **`nt` (note) has two encodings.** Creation/baseline: `{"_t":"tx","v":<full text>}`.
  A later edit sends an **operational patch**: `{"_t":"tx","ps":[{"r":<replacement>,
  "p":<pos>,"l":<len>}]}`. Because EDITs replace the whole `nt` field, the full `v`
  is lost unless you apply the patches against the running text. This client does a
  best-effort join of patch replacements; full OT reconstruction is a TODO.
- **`rr` (recurrence) is a structured dict, not an XML string** in this schema,
  e.g. `{"ia":<ts>,"rrv":4,"tp":1,"of":[{"dy":14}],"fu":8,"sr":<ts>,"ed":<ts>}`.
  Map this out by diffing a few repeat configurations.
- **`ato` is a reminder** in seconds-since-midnight; `icsd` is the instance creation
  start date for repeats; `ti`/`tir` are Today-list ordering helpers.
- **~35% of Task6 entities on a real, multi-year account were EDIT-only
  fragments** — never given a `t:0` NEW this client could see (compaction, or
  genesis not captured; see §8 item 1's neighbor for what could confirm
  which). Most still carried `status`/`destination` (a stray "mark done" or
  "move to Anytime" touches those), which is why `from_entities()` used to
  accept any one of `type`/`status`/`destination` as "real enough" to show as
  an open task — materializing hundreds of floating, untitled, unattached rows
  with nothing to attach them to a real object. The fix (see `model.py`)
  requires `type` presence specifically (or a title, or a project/area/heading
  parent) — `status`/`destination` alone are too common on partial edits to be
  real signal.
- **A repeating task is represented as a template entity (carries its own
  `rr`) plus several separately-materialized instance entities (each carries
  `rt: [<template uuid>]`).** Things Cloud pre-generates instances ahead of
  time — on one real account, a single recurring chore had 7 simultaneously
  "open" instances in the log, with scheduled dates spanning almost 2 years,
  because none had ever been completed. The template entity itself is never a
  real occurrence and must not be shown as a task; among instances sharing one
  `rt`, only one should be visible as "the" open task at a time (closed
  instances all remain, for Logbook). Which one to pick is unconfirmed — see
  `model.py`'s `_select_current_recurring_instance` and §8 item 8.

## 8. Help wanted: specific captures still needed

The single most useful thing you can do to move this forward is exactly the
loop §2.2 already describes — make one change in the real Things app, diff the
tail of the log — but mechanized and redacted so what you capture is safe to
hand back. Run:

```bash
python3 tools/capture.py
```

It signs in, reads your current head index, prompts you to make one change (a
different one each round, working down the list below), and prints the new log
items with all free text redacted (same redaction as `tools/diagnose.py`:
keys and structure preserved, string values replaced by `<str len=N>`), so the
output can be pasted directly into a conversation with Claude Code without
sharing any of your actual task titles, notes, or tags.

Ordered by how much they unblock, most valuable first:

1. ~~Delete a task, then diff the tail~~ — **done**, the *read* side is
   confirmed (§6): empty `t:2` is a deletion, on this account 100% of the
   time. Still open if you want it: **restore a deleted task** in the real
   app and capture what that looks like (an explicit `tr:false` EDIT is
   `_apply()`'s current assumption — see the "later tr:false wins" case in
   `tests/test_pagination.py::test_t2_and_fragments` — but that's untested
   against a real restore). And separately, if you ever implement a delete
   *write* action: capture what the real app sends when *it* deletes
   something, to confirm it matches `evanpurkhiser/things3-cloud`'s
   `{"t":2,"e":"Task6","p":{}}` before trusting it — reading and writing the
   same signal correctly are two different confirmations.
2. **A daily repeat, a "every 2 weeks on Tue/Thu" repeat, and a "monthly on
   the last day" repeat** — three separate captures. Diff the `rr.of` field
   across all three; that's the piece neither reference project fully cracked.
3. **Create a new tag**, and separately **nest a tag under another tag** —
   confirms the `Tag4`/`pn` shape above against this codebase's own account.
4. **Create a new area**, and **apply a tag directly to an area** — confirms
   `Area3`/`tg`.
5. **Set a reminder time (`ato`) on a task.** Simple field, quick to confirm.
6. **Reorder two tasks in the same list** (drag one above another) — confirms
   the `ix` gap-rebalancing behavior against a real client, not just the Rust
   CLI's own fixtures.
7. **Create a brand-new task from the real Mac/iOS app** and capture its NEW
   payload verbatim — resolves the `nt: null` vs. `nt: {...}` disagreement
   above, and confirms whether the ID it gets matches the SHA1+base58 scheme.
8. **Not a wire capture — a UI observation.** Find a repeating task in the real
   Mac/iOS app that's been running long enough to have a stale, un-completed
   backlog (or deliberately skip completing one for a few cycles), and note
   exactly which single occurrence the app shows you, and where (Today?
   Upcoming? does it show a date from months/years ago, or does it show
   "today"?). `model.py`'s `_select_current_recurring_instance` currently
   guesses "the open instance closest to today, preferring overdue over
   future" — found from a real account that had this exact backlog (a chore
   with 7 pre-materialized open instances spanning ~2 years), but never
   confirmed against what the real app actually surfaces for the same data.
   Also check: complete every currently-open instance of a repeat and note
   whether the log immediately gets a fresh open instance, or whether a repeat
   can sit instance-less for a while. `_collapse_recurring_instances` currently
   assumes the former (Things eagerly pre-generates the next instance) — if
   that's wrong, a repeat could briefly have zero visible representation
   anywhere, including the sidebar for a repeating project.

For each capture, paste the tool's redacted output plus a one-line description
of exactly what you did in the real app (e.g. "repeat every 2 weeks on Tue and
Thu, starting today"). That pairing — the real UI action next to its exact
wire encoding — is what turns a guess into a confirmed field.

---

## 9. Smart-list categorization, confirmed against a real account and phone

This is the single most-corrected part of the whole client. The original
`categorize()` was a straight port of a much simpler model (`sched <= today`
means Today) that turned out to be wrong in several specific, now-confirmed
ways. All of this was found by directly comparing this app's output against
the user's real Things app — a manually-typed dump of Inbox/Today/Upcoming
plus phone screenshots of Anytime/Someday/every area and project — not
inferred from the wire format alone.

### 9.1 Today is "exactly today", not "on or before today"

**Wrong assumption:** any task with `scheduled_date <= today` belongs in
Today, so overdue items pile up there forever.

**Confirmed correct:** Today is `scheduled_date == today` exactly, or
`deadline == today` exactly (the deadline rule is inferred by symmetry — see
§9.3 — not as directly confirmed as the scheduled_date one), or the "waiting"
instance of an active repeat (§9.4). A one-off task scheduled weeks, months,
or years ago that was never completed just sits quietly in **Anytime** —
Things does not keep re-surfacing it in Today. On the account this was tested
against, this alone took Today from 48 phantom items down to a handful.

One consequence worth knowing: a task can be truly stuck (server has no
completion/deletion event for it at all — see §9.6) and this rule still
produces the right *user-facing* result, because Anytime is where it quietly
belongs regardless of whether we know it was actually dealt with.

### 9.2 Someday + a future date = Upcoming, not Someday

**Confirmed:** every real Upcoming item with a far-future date (some as far
out as 2029–2035) turned out to carry `destination == SOMEDAY` (`st: 2`), not
Anytime. Picking a specific date for a Someday task "graduates" it into
Upcoming for display purposes, even though the underlying `st` field stays
`2`. Only a Someday task with a *stale past* date (or no date at all) stays in
Someday — confirmed separately: a task explicitly moved to Someday can retain
a leftover pre-move `scheduled_date` from before that move, and that stale
date must not pull it back into Today or Upcoming.

Precedence (see `Database.categorize()` for the literal order, comments
explain each step): an overdue-exactly-today deadline > a future date (→
Upcoming, regardless of destination) > a waiting active repeat (§9.4,
regardless of destination) > Someday > scheduled-exactly-today > Anytime.

### 9.3 Deadlines: same exactly-today rule, lower confidence

`due_date == today` pulls a task into Today (and overrides Someday) the same
way `scheduled_date == today` does. This was inferred by symmetry after two
real overdue-deadline tasks (deadlines months to years in the past) turned up
in this app's Today list but were not in the user's real 5-item Today — it
was not independently asked about and confirmed the way the scheduled_date
rule was. If a future capture contradicts this, `due` is checked first in
`categorize()` and is the one line to change.

### 9.4 Recurring tasks: "waiting" repeats, not every `rt` reference

Getting duplicates and phantom Today items down to nearly nothing needed three
separate, layered fixes, all in `model.py`'s `_collapse_recurring_instances`
/ `_select_current_recurring_instance`:

1. **Not every task with a `repeating_template` (`rt`) reference is an active
   repeat.** Confirmed: several one-off tasks carried an `rt` reference to a
   template whose own `instance_creation_count` (`icc`) was `0` and which
   only ever produced that one instance — a repeat defined once and never
   actually run. Treating any `rt` reference as "perpetually waiting" pulled
   these into Today alongside genuine repeats. A chain only gets the
   "waiting" treatment if it's genuinely active: `icc > 0`, more than one
   distinct instance observed, **or** `instance_creation_start_date` (`icsd`)
   within about the last month of today (see point 3 — `icc` itself is often
   lost to compaction, `icsd` survives better).
2. **An active repeat's "waiting" instance shows in Today regardless of how
   stale its own date is**, and regardless of its own `destination` — an
   overdue waiting instance commonly carries `destination == SOMEDAY` too
   (apparently Things' way of saying "no fixed date"), so the waiting-repeat
   check must run *before* the Someday check, not after. But a repeat
   instance with a **future** date is a completely normal Upcoming item, not
   "waiting" — confirmed: a repeat whose next occurrence already has a real
   future date shows in Upcoming exactly like a one-off task would.
3. **A genuinely active chain can have zero open instances in the readable
   log at all.** Confirmed for three separate repeats simultaneously on one
   account: each had exactly one referencing entity in the *entire* history
   log, and it was completed — no open instance existed anywhere, yet the
   real app still showed all three as "waiting" in Today. The template
   entity itself becomes the stand-in placeholder in this case (it's dropped
   normally once an open instance exists to represent the chain — same
   treatment as a heading being dropped in favor of `heading_title` — but
   kept when there's nothing to drop it in favor of). This also means the
   fragment-materialization gate (§7) needed one more exception: a template's
   own `t:0` NEW can be compacted away just like anything else, leaving it
   title-less and type-less, so being *referenced* as another task's `rt` is
   now itself accepted as "real enough" evidence, alongside title/type/
   parent. When the template has no title of its own, it borrows one from any
   instance that referenced it (same conceptual task, different uuid).

`instance_creation_start_date` ticking forward by ~1 day per EDIT (dozens of
consecutive daily-incrementing edits touching only `icsd` were observed on
every genuinely active template) is worth knowing about on its own: it's
strong, compaction-resistant evidence that Things Cloud is still actively
maintaining a chain, independent of whether `icc` itself survived.

### 9.5 Same-titled tasks without a shared `rt` are real duplicates

Confirmed: title collisions with no `repeating_template` link in common (e.g.
several manually-created "Buy new glasses" tasks) are genuine, independent,
user-created items, not a bug. Only entities Things Cloud itself linked via
`rt` get collapsed.

### 9.6 Some data loss is real and permanent, not a bug here

Two categories of item were found where the *server's own history log*
contains no signal at all to explain the user's real state:

- **Deletions.** Every `t:2` event observed on a real account (256, zero
  exceptions) had an empty payload, and cross-referencing specific uuids
  against real tasks showed ordinary titled tasks the user had deleted on
  another device still showing as active — see §6 for the full resolution
  (empty `t:2` is now treated as a deletion). Fixed, not a permanent gap.
- **Completions that predate what the server retains.** Confirmed directly by
  the user: a task ("Convert Spotify Playlist to Apple Music") was completed
  in 2024 and correctly shows in the real Logbook — but its entire raw event
  history in this account's readable log is just `NEW` + one unrelated EDIT;
  no completion event exists anywhere. A second task was, in the user's own
  words, "probably completed and purged automatically by the Logbook at some
  point" — it doesn't exist anywhere in the account any more, real or
  synced. Both, plus seven similar Inbox items, have only 1–2 total events
  *ever* in the raw log (just creation) — nothing to recover. Things Cloud
  appears to prune very old resolved items from what a fresh client can see
  at all. §9.1's "Today is exactly-today" rule is what keeps this from being
  user-visible for scheduled tasks (an unrecoverable stale item just sits in
  Anytime); there's no equivalent trick for Inbox, which has no date-based
  narrowing at all, so a handful of these can still surface there
  permanently. This is not fixable client-side; do not spend time on it
  without new evidence that the server retains more than this.

### 9.7 Repeat chains missing their freshest instance — confirmed live, cause now understood

Two repeat chains ("Change the pillowcase", "Tech Day") showed a stale
"waiting" instance in Today when the real app showed them correctly in
Upcoming with a near-future date. This has now been reproduced live against
the account's current cloud state (not just the earlier snapshot), with the
full chain visible:

Template `6jpr…` ("Change the pillowcase"): `instance_creation_count = 38`
and `instance_creation_start_date = 2026-08-07`, one day ahead of "today" —
set to a future date, though its own `modification_date` (~2025-10-30) shows
it hasn't been touched in nine months, so this is a value set once well in
advance, not something ticking daily on this particular chain (§9.4's
daily-tick observation was on other templates). Liveness here instead rests
on `icc = 38` plus a completion as recent as 2026-07-30. Its **only two open
instances** in the readable log are dated 2025-12-26 and 2026-06-20 — both
already stale. The correct next occurrence, 2026-08-06 (confirmed exactly:
last completion 2026-07-30 + the chain's `fa:7, fu:16` = +7 days, see above),
**exists nowhere in the log as a materialized entity**, even immediately after
a fresh incremental sync to the account's current head.

**Conclusion: this is not a client bug, and — on this specific chain — not a
sync-timing race either.** The 2026-07-30 completion event *is* present in
the log, so a week's worth of propagation has demonstrably already happened
on this chain by the time of this check; if the server materialized the next
occurrence at completion time the way it materializes other instances, it
would have arrived alongside that completion event, and it hasn't (contrast
§9.9, which *is* a genuine propagation gap — for a different, freshly-created
item that hadn't synced at all). The server genuinely does not keep a
materialized Task6 entity for "the next occurrence" of an after-completion
repeat sitting ready in the log. The real Things apps must be computing the
displayed Upcoming date procedurally — `last completion_date + fa` scaled by
`fu` — rather than reading it off a server-provided instance.
`_select_current_recurring_instance` in `model.py`
only ever picks among instances that exist in the log, so it cannot produce
this date no matter how it's tuned; the fix is to *compute* the next
occurrence from the template's `recurrence_rule` + whichever instance has the
latest `completion_date`, and prefer that computed date over any
stale/server-materialized open instance when it's more recent.

**✅ Implemented**, at the user's direction, in
`_collapse_recurring_instances` (see its docstring) — an active `tp:1`
chain's surviving instance has its `scheduled` overridden with `last
completion_date + fa` (scaled by `fu`) whenever that's later than the best
date already on the log's open instances for that chain. Re-verified against
the real account after implementing: "Change the pillowcase" now computes to
2026-08-06, matching the real app exactly (see above). The evidence bar
behind this is honestly one clean live confirmation, not "several chains
independently confirmed" — of 19 templates found with a live
`recurrence_rule` on this account, only the pillowcase chain had both a
recent completion *and* a computed date landing in the future, the only real
test of "does the formula correctly predict Upcoming placement." Every other
`tp:1` template either had no completed instance at all, or a computed date
still safely in the past (consistent with the formula but not an independent
placement test). "Tech Day" — the second case originally flagged here —
turns out to have **no `recurrence_rule` at all**: its template
(`EK7jgkDLVvurDUa9jcWvpP`) has `instance_creation_count = None`,
`instance_creation_start_date = None`, and is absent from the set of
templates carrying `rr`. It isn't a counterexample to the formula; its
template is simply compacted past the point where the formula's inputs still
exist (same compaction pattern as §7/§9.6), so nothing can be computed for it
— it correctly falls back to its stale open instance, still wrong per the
real app, unfixable without more data. If a future chain contradicts the
formula, it's isolated to the `computed_next` block in
`_collapse_recurring_instances` — nothing else needs to change.

### 9.8 `today_index`/`today_index_reference_date` are NOT a live Today-membership flag

Plausible-looking dead end, ruled out with real data. The hypothesis was:
maybe a task that's "just been added to Today" (no date, no repeat) is
distinguished from an ordinary Anytime task by `ti`/`tir` being set — i.e. a
persisted "pin to Today" flag independent of `scheduled_date`, which would
explain the user-reported behavior that a task added straight into Today
stays there until moved/deleted/rescheduled, not just for the day it was
added.

**Disproven directly:** on a real account, 56 currently-*open* tasks carry a
non-zero `today_index`, including ones with `scheduled_date` in 2031, 2024,
and every year between — plainly not currently in Today — and some with
*negative* `today_index` values (e.g. `-2988`). `today_index_reference_date`
on these is usually just a stale copy of whatever `scheduled_date` was at the
time the task last appeared in Today. Both fields are a **persisted historical
sort position**, written whenever a task is in Today and never cleared when it
later leaves — not a live membership signal. Also confirmed: `Task6`'s field
map has zero undecoded short keys across 1440 real entities, so there is no
other hidden field to check either — whatever encodes "pinned to Today
regardless of date" (if anything does, at the wire level, distinct from
`scheduled_date` itself) is not among the fields this client currently
decodes and reads. This remains an open question, not a solved one; don't
re-guess `today_index` as the answer without new evidence. Note the one task
that actually exhibits this sticky-Today behavior in real life — the one this
hypothesis was originally trying to explain — is the task from §9.9 that
never reached the log at all, so the wire-level question is genuinely
unexamined, not answered "no."

### 9.9 A task visible in the live app can be entirely absent from the cloud history log

Different from §9.6's *permanent, old* pruning — this is a **fresh, un-pushed
item**. A task ("Reverse engineer app status on magnet axiom") appeared in a
phone screenshot of Today, taken same-day, but does not exist anywhere in the
account's history log — confirmed by logging in fresh and incrementally
syncing all the way to the account's current head (`5180 → 5182`, 2 new
items, neither this task). The account's single most-recent
`modification_date` across all 1440 entities was hours *before* the
screenshot was taken. The task simply hadn't propagated from whichever device
created it to Things Cloud by the time this was checked — a live sync
propagation gap, not a compaction/retention issue. Worth remembering when a
diff against a fresh phone screenshot doesn't match a fresh sync: check
whether the *other device* has actually pushed yet before assuming this
client's replay is wrong.

---

## 10. Quick reference

Enums (inside a `Task6` payload):

```
type      tp : 0 task   1 project   2 heading
status    ss : 0 todo   2 cancelled 3 complete
dest      st : 0 inbox  1 anytime/today  2 someday
```

Enums (inside a recurrence rule `rr`, i.e. `Task6.rr`, confirmed §6/§9.7):

```
repeat_type       tp : 0 fixed-schedule   1 after-completion
frequency_unit    fu : 4 year   8 month   16 day   256 week
                       (== legacy Apple NSCalendarUnit raw values)
```

`rr`'s sub-keys (`tp`/`fu`/`fa`/`of`/`sr`/`ia`/`ed`/`rc`/`ts`/`rrv`) are raw
wire abbreviations, not run through `sync/protocol.py`'s `FIELD` map — only
the outer `rr` key itself is. Don't go looking for `frequency_unit` in
`FIELD` and conclude this doc is stale when it isn't there.

Most-used fields: `tt` title · `ss` status · `st` destination · `sr`
scheduled(when) · `dd` deadline · `sp` completed · `cd`/`md` created/modified ·
`pr` project · `ar` area · `agr` heading · `tg` tags · `nt` note · `sb` evening ·
`ato` reminder · `ts` checklist-parent. The full map lives in
`sync/protocol.py: FIELD`.

Update kinds: `t:0` NEW (full object) · `t:1` EDIT (partial delta).

Timestamps are Unix seconds, UTC. Dates that are "all-day" (when/deadline) are
ints; created/modified are floats.
