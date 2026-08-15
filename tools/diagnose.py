#!/usr/bin/env python3
"""Diagnose a Things Cloud sync without leaking your task content.

It signs in, pages the WHOLE raw history log, and prints structural statistics
plus a few REDACTED sample events (keys kept, all free text replaced with its
length). Nothing it prints contains your titles, notes, or tags, so the output is
safe to paste back.

Run from the project root:

    python3 tools/diagnose.py

It will prompt for your Things Cloud email and password (password is hidden and
is only sent to cloud.culturedcode.com, exactly as the app does).
"""

import getpass
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thingsgnome.sync import login                       # noqa: E402
from thingsgnome.sync.client import ThingsReadClient     # noqa: E402

# fields whose values are free text we must NOT print
TEXT_FIELDS = {"tt", "title", "nt", "note", "v", "tg", "tags"}


def redact(value, key=None):
    """Return a structure-preserving, content-free view of a value."""
    if key in TEXT_FIELDS:
        if isinstance(value, str):
            return f"<str len={len(value)}>"
        if isinstance(value, list):
            return f"<list len={len(value)}>"
        if isinstance(value, dict):
            return {k: redact(v, k) for k, v in value.items()}
        return "<redacted>"
    if isinstance(value, str):
        # short tokens (enum-like / type names) are safe; long strings redacted
        return value if len(value) <= 24 else f"<str len={len(value)}>"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value][:4]
    return value


def main():
    email = os.environ.get("THINGS_EMAIL") or input("Things Cloud email: ").strip()
    password = os.environ.get("THINGS_PASSWORD") or getpass.getpass("Password: ")

    print("\nSigning in…")
    account = login(email, password)
    print(f"  history_key        : …{account.history_key[-6:]} (len {len(account.history_key)})")
    print(f"  session head_index : {account.head_index}")

    client = ThingsReadClient(account)

    top_level_keys = set()
    history_keys_seen = set()
    server_head = 0
    pages = 0
    fetched = 0

    t_counter = Counter()
    e_counter = Counter()
    te_counter = Counter()        # (t, e) cross-tab
    uuids = set()
    state_bearing = set()         # uuids ever given a NEW or a t=2 WITH payload

    task_status = Counter()
    task_dest = Counter()
    task_type = Counter()
    task_empty_title = 0
    task_trashed = 0

    sample_new = []
    sample_edit = []
    sample_t2 = []

    index = 0
    for _ in range(client._max_pages):
        page = client._fetch_page(index)
        pages += 1
        top_level_keys.update(page.keys())

        # discover any history-key-rotation field, whatever it's called
        for k, v in page.items():
            if "history" in k.lower() and isinstance(v, str):
                history_keys_seen.add(f"{k}=…{v[-6:]}")

        sh = page.get("current-item-index")
        if isinstance(sh, int):
            server_head = max(server_head, sh)

        items = page.get("items") or []
        if not items:
            break

        for item in items:
            if not isinstance(item, dict) or not item:
                continue
            uuid, body = next(iter(item.items()))
            if not isinstance(body, dict):
                continue
            fetched += 1
            uuids.add(uuid)
            t = body.get("t")
            e = body.get("e", "?")
            t_counter[t] += 1
            e_counter[e] += 1
            te_counter[(t, e)] += 1
            p = body.get("p", {}) or {}

            if t == 0 or (t == 2 and p):
                state_bearing.add(uuid)

            if e == "Task6":
                if t == 0:  # NEW carries full state worth tallying
                    task_status[p.get("ss")] += 1
                    task_dest[p.get("st")] += 1
                    task_type[p.get("tp")] += 1
                    if not (p.get("tt") or "").strip():
                        task_empty_title += 1
                    if p.get("tr"):
                        task_trashed += 1

            if t == 0 and len(sample_new) < 3:
                sample_new.append({uuid[:6] + "…": {"t": t, "e": e, "p": redact(p)}})
            elif t == 1 and len(sample_edit) < 4:
                sample_edit.append({uuid[:6] + "…": {"t": t, "e": e, "p": redact(p)}})
            elif t == 2 and len(sample_t2) < 6:
                sample_t2.append({uuid[:6] + "…": {"t": t, "e": e,
                                  "p_empty": not bool(p), "p": redact(p)}})

        index += len(items)

    print("\n── log fetch ─────────────────────────────────────────────")
    print(f"  pages fetched            : {pages}")
    print(f"  raw items processed      : {fetched}")
    print(f"  server current-item-index: {server_head}   (the log head)")
    if server_head and fetched < server_head - 1:
        print(f"  ⚠ TRUNCATION: fetched {fetched} but head is {server_head} "
              f"→ {server_head - fetched} events not read")
    else:
        print("  ✓ reached the log head")
    print(f"  distinct uuids           : {len(uuids)}")

    print("\n── top-level response keys (across pages) ───────────────")
    print("  " + ", ".join(sorted(top_level_keys)))
    if history_keys_seen:
        print("  history-key fields seen  : " + ", ".join(sorted(history_keys_seen)))
        print(f"  (we synced against       : …{account.history_key[-6:]})")

    print("\n── event kinds (t) ──────────────────────────────────────")
    for k, v in sorted(t_counter.items(), key=lambda kv: str(kv[0])):
        label = {0: "NEW", 1: "EDIT"}.get(k, "OTHER")
        print(f"  t={k!r:<6} {label:<6}: {v}")

    print("\n── entity types (e) ─────────────────────────────────────")
    for k, v in e_counter.most_common():
        print(f"  {k:<16}: {v}")

    print("\n── kind × entity cross-tab ──────────────────────────────")
    for (t, e), v in sorted(te_counter.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        label = {0: "NEW", 1: "EDIT", 2: "T2"}.get(t, "OTHER")
        print(f"  t={t!r:<5} {label:<5} {e:<16}: {v}")

    edit_only = uuids - state_bearing
    print("\n── fragment check ───────────────────────────────────────")
    print(f"  distinct uuids                 : {len(uuids)}")
    print(f"  with a NEW or t=2-with-payload : {len(state_bearing)}")
    print(f"  ⚠ EDIT-only (no base state)    : {len(edit_only)}")

    print("\n── Task6 NEW-event state histograms ─────────────────────")
    print(f"  empty title (at NEW)     : {task_empty_title}")
    print(f"  trashed (at NEW)         : {task_trashed}")
    print(f"  status ss  (0 todo,2 cancelled,3 done): {dict(task_status)}")
    print(f"  destin. st (0 inbox,1 anytime,2 someday): {dict(task_dest)}")
    print(f"  type tp    (0 task,1 project,2 heading): {dict(task_type)}")

    print("\n── redacted sample NEW events ───────────────────────────")
    for s in sample_new:
        print("  " + repr(s))
    print("\n── redacted sample EDIT events ──────────────────────────")
    for s in sample_edit:
        print("  " + repr(s))

    print("\n── redacted sample t=2 events (the dropped ones) ────────")
    for s in sample_t2:
        print("  " + repr(s))

    print("\nDone. The above contains no task text and is safe to share.")


if __name__ == "__main__":
    main()
