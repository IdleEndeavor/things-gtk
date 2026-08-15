#!/usr/bin/env python3
"""Mechanize the "make one change, diff the tail" capture loop from
REVERSE-ENGINEERING.md §2.2, for the specific captures listed in §8.

For each round: pick what you're about to do in the REAL Things app, make that
ONE change there, then press Enter here. This tool reads the raw tail of your
history log since your last-known head index and prints it with all free text
REDACTED (same scheme as diagnose.py: keys and structure preserved, string
values replaced by their length) -- the output is safe to paste back into a
conversation with Claude Code without sharing any of your actual task titles,
notes, or tags.

Run from the project root:

    python3 tools/capture.py

It will prompt for your Things Cloud email and password (password is hidden
and is only sent to cloud.culturedcode.com, exactly as the app does).
"""

import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from thingsgnome.sync import login                       # noqa: E402
from thingsgnome.sync.client import ThingsReadClient     # noqa: E402

# Mirrors diagnose.py's redact() -- duplicated rather than imported so this
# script stays runnable standalone (tools/ isn't a package).
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
        return value if len(value) <= 24 else f"<str len={len(value)}>"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value][:4]
    return value


CAPTURES = [
    "Delete a task, then restore it (if the Mac app can) -- resolves the "
    "t:2-vs-tr delete conflict in REVERSE-ENGINEERING.md §6",
    "Set a daily repeat on a task",
    "Set a repeat 'every 2 weeks on Tue/Thu'",
    "Set a repeat 'monthly on the last day'",
    "Create a new tag",
    "Nest a tag under another tag",
    "Create a new area",
    "Apply a tag directly to an area",
    "Set a reminder time on a task",
    "Reorder two tasks in the same list (drag one above another)",
    "Create a brand-new task from the real Mac/iOS app",
]


def _read_raw_tail(client: ThingsReadClient, start_index: int) -> tuple[list, int]:
    """Page raw (undecoded) items from start_index, following the same
    advance-by-items-returned rule as ThingsReadClient.replay() -- see
    REVERSE-ENGINEERING.md §1.3 for why that matters."""
    items: list = []
    index = start_index
    head = start_index
    for _ in range(client._max_pages):
        page = client._fetch_page(index)
        page_items = page.get("items") or []
        server_head = page.get("current-item-index")
        if isinstance(server_head, int):
            head = max(head, server_head)
        if not page_items:
            break
        items.extend(page_items)
        index += len(page_items)
        if head and index >= head:
            break
    return items, max(index, head)


def main():
    email = os.environ.get("THINGS_EMAIL") or input("Things Cloud email: ").strip()
    password = os.environ.get("THINGS_PASSWORD") or getpass.getpass("Password: ")

    print("\nSigning in…")
    account = login(email, password)
    client = ThingsReadClient(account)
    print(f"  head_index: {account.head_index}\n")

    print("Suggested captures (see REVERSE-ENGINEERING.md §8 for why each matters):")
    for i, label in enumerate(CAPTURES, 1):
        print(f"  {i}. {label}")
    print(
        "\nAt each prompt: pick a number (or type your own label), make that ONE "
        "change in the real Things app, wait for it to sync, then press Enter.\n"
        "Leave the prompt blank to stop.\n"
    )

    results = []
    out_path = Path.cwd() / "capture-log.json"

    while True:
        choice = input("Capture # (or label, blank to quit): ").strip()
        if not choice:
            break
        if choice.isdigit() and 1 <= int(choice) <= len(CAPTURES):
            label = CAPTURES[int(choice) - 1]
        else:
            label = choice

        print(f"\n→ {label}")
        print("  Re-checking the current head index…")
        fresh_account = login(email, password)
        start_index = fresh_account.head_index

        input("  Make that ONE change now, let it sync, then press Enter… ")

        raw_items, new_head = _read_raw_tail(client, start_index)
        redacted = [
            {uuid: redact(body, None) for uuid, body in item.items()}
            for item in raw_items
            if isinstance(item, dict)
        ]

        print(f"  Captured {len(redacted)} raw item(s), head {start_index} -> {new_head}:")
        for item in redacted:
            print("   ", json.dumps(item))

        if not redacted:
            print(
                "  (Nothing captured -- the change may not have synced yet. Try "
                "again, or check the Things app shows it as synced.)"
            )

        results.append({
            "label": label,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "start_index": start_index,
            "new_head": new_head,
            "items": redacted,
        })
        out_path.write_text(json.dumps(results, indent=2))
        print(f"  (saved so far to {out_path})\n")

    if results:
        print(f"\nDone. {len(results)} capture(s) saved to {out_path}.")
        print("This file contains no task text and is safe to paste or share.")
    else:
        print("\nNo captures recorded.")


if __name__ == "__main__":
    main()
