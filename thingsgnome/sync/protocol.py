"""
Things Cloud wire-protocol constants and field map.

Everything in this file is the result of reverse-engineering the traffic that the
official Things for Mac client exchanges with cloud.culturedcode.com. None of it is
documented or supported by Cultured Code. It is built for personal interoperability
(reading your *own* account from a device Things does not officially support) and may
break at any time if Cultured Code change the protocol.

The heavy lifting of decoding the field names was done by the community, principally
the `disrupted/things-cloud-api` and `nicolai86/things-cloud-sdk` projects. This module
re-states that mapping in one place so the rest of the app can stay readable.

------------------------------------------------------------------------------------
HOW THE PROTOCOL WORKS (so future-you can extend it)
------------------------------------------------------------------------------------
1. Auth, step 1 - resolve the account:
       GET  https://cloud.culturedcode.com/version/1/account/{email}
       Header:  Authorization: Password {url-encoded-password}
   -> JSON containing "history-key" (a UUID). That key is the handle to all your data.

2. Auth, step 2 - open a session (gives the current head index):
       POST https://cloud.culturedcode.com/api/account/login/getT3SharedSession
       Header:  Authorization: B64SON {base64(json({"ep":{"e":email,"p":password}}))}
   -> JSON with "headIndex" (int) and "historyKeySessionSecret".

3. Read - the data is an APPEND-ONLY EVENT LOG ("history"), not a snapshot:
       GET  https://cloud.culturedcode.com/version/1/history/{history_key}/items
            ?start-index={offset}
   -> { "current-item-index": N,
        "items": [ { "<22-char-uuid>": { "t": 0|1, "e": "Task6", "p": {...} } }, ... ] }
   To get current state you replay the log from index 0:
       t == 0  (NEW)  -> payload "p" is the FULL object; create it.
       t == 1  (EDIT) -> payload "p" is a PARTIAL delta; apply it onto the existing item.
   "current-item-index" is the HEAD (total length) of the log and is ~constant
   across pages -- it is NOT the next offset. Page through by advancing
   start-index by the NUMBER OF ITEMS each page returned, until a page comes
   back empty (or start-index reaches the head). Advancing straight to
   current-item-index consumes only the first batch and gives a stale snapshot.

4. Write (not used by the read-only MVP):
       POST .../history/{history_key}/commit?ancestor-index={offset}&_cnt=1
            body: { "<uuid>": { "t": 0|1, "e": "Task6", "p": {...} } }
"""

from __future__ import annotations

API_BASE = "https://cloud.culturedcode.com/version/1"
ACCOUNT_URL = "https://cloud.culturedcode.com/version/1/account/{email}"
SESSION_URL = "https://cloud.culturedcode.com/api/account/login/getT3SharedSession"

# Default App-Id observed from the Mac client. Reads appear not to validate the
# User-Agent strictly; if a future server build does, capture the real one (see
# REVERSE-ENGINEERING.md) and override via Preferences / THINGS_USER_AGENT.
APP_ID = "com.culturedcode.ThingsMac"
DEFAULT_USER_AGENT = "ThingsMac/3.20 ThingsGNOME (personal interop client)"
SCHEMA = "301"


def sync_headers(user_agent: str = DEFAULT_USER_AGENT) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Charset": "UTF-8",
        "Accept-Language": "en-gb",
        "Host": "cloud.culturedcode.com",
        "User-Agent": user_agent,
        "Schema": SCHEMA,
        "Content-Type": "application/json; charset=UTF-8",
        "App-Id": APP_ID,
        "App-Instance-Id": f"-{APP_ID}",
        "Push-Priority": "5",
    }


# --- entity types seen in the log ------------------------------------------------
ENTITY_TASK = "Task6"            # tasks, projects and headings all use this
ENTITY_CHECKLIST = "ChecklistItem3"

# --- update kinds ----------------------------------------------------------------
UPDATE_NEW = 0
UPDATE_EDIT = 1
# A third kind (t == 2) appears in real logs. It is state-bearing when it carries
# a payload (a baseline/snapshot of an entity) and a deletion when the payload is
# empty. The read client handles both: merge the payload if present, else drop the
# entity. (Permanent deletions are also represented by `Tombstone2` entities.)
UPDATE_BASELINE_OR_DELETE = 2

# --- enums inside a Task6 payload ------------------------------------------------
TYPE_TASK = 0
TYPE_PROJECT = 1
TYPE_HEADING = 2

STATUS_TODO = 0
STATUS_CANCELLED = 2
STATUS_COMPLETE = 3

DEST_INBOX = 0
DEST_ANYTIME = 1     # Anytime / Today / Evening live here, distinguished by scheduled_date
DEST_SOMEDAY = 2

# --- the cryptic two-letter field keys, mapped to human names --------------------
# Only the keys we actually use are commented in detail; the rest are kept so the
# replay can round-trip and so you can inspect them while extending the app.
FIELD = {
    "ix": "index",                          # ordering within a list
    "tt": "title",
    "ss": "status",                         # STATUS_*
    "st": "destination",                    # DEST_*
    "cd": "creation_date",                  # float unix ts
    "md": "modification_date",              # float unix ts
    "sr": "scheduled_date",                 # int unix ts | None  ("when")
    "tir": "today_index_reference_date",
    "sp": "completion_date",                # int unix ts | None
    "dd": "due_date",                       # int unix ts | None  (deadline)
    "tr": "trashed",                        # bool
    "icp": "instance_creation_paused",      # bool (true for projects)
    "pr": "projects",                       # [project_uuid] parent project
    "ar": "areas",                          # [area_uuid] parent area
    "sb": "evening",                        # bool-as-bit
    "tg": "tags",                           # [tag_uuid] or [str]
    "tp": "type",                           # TYPE_*
    "dds": "due_date_suppression_date",
    "rt": "repeating_template",
    "rmd": "repeater_migration_date",
    "dl": "delegate",
    "do": "due_date_offset",
    "lai": "last_alarm_interaction_date",
    "agr": "action_group",                  # [heading_uuid] parent heading
    "lt": "leaves_tombstone",
    "icc": "instance_creation_count",
    "ti": "today_index",
    "ato": "reminder",                      # seconds-since-midnight | None
    "icsd": "instance_creation_start_date",
    "rp": "repeater",
    "acrd": "after_completion_reference_date",
    "rr": "recurrence_rule",                # XML string | None
    "nt": "note",                           # {"_t":"tx","ch":0,"v":<text>,"t":1}
    "xx": "xx",                             # opaque {"sn":{},"_t":"oo"}
    "ts": "tasks",                          # checklist item -> [parent_task_uuid]
}


def decode_fields(raw: dict) -> dict:
    """Translate a raw two-letter-keyed payload into a human-keyed dict.

    Unknown keys are preserved under their original name so nothing is silently
    lost while you are reverse-engineering new fields.
    """
    out: dict = {}
    for k, v in raw.items():
        out[FIELD.get(k, k)] = v
    return out
