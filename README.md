# Things for GNOME

A native GTK4 / libadwaita desktop app for **viewing your Things 3 tasks on
Linux**. It talks directly to Things Cloud using a reverse-engineered, read-only
implementation of the same sync protocol the official Things for Mac client uses,
so you can see everything from your iPhone/Mac on your Fedora laptop without
opening either device.

It can also load the JSON your existing iOS Shortcut exports, so it works whether
or not you want to sign in to the cloud.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported
> by Cultured Code. It reads *your own* account for personal interoperability.
> The protocol is undocumented and may change at any time.

![A native GNOME window: sidebar with smart lists and areas, task list with
deadline and tag chips.](docs/screenshot.png)

## What it does (v0.1)

- Signs in to Things Cloud and **syncs your data read-only**.
- Smart lists: **Inbox, Today, Upcoming, Anytime, Someday, Logbook** — categorised
  with the same rules as the Mac app.
- **Areas** in the sidebar, each expanding to its projects.
- Project / area views with headings, notes, checklists, tags, deadlines and
  scheduled dates.
- Open / Done / All filtering and full-text search.
- Works offline: the last sync is cached locally and painted instantly on launch.
- Also opens a Shortcuts **JSON export** (File the export anywhere and load it).

It is deliberately **read-only** for now. See
[Why read-only](#why-read-only) and [REVERSE-ENGINEERING.md](REVERSE-ENGINEERING.md)
for how to add writing (completing tasks, creating tasks, scheduling…) safely.

## Install on Fedora

You need GNOME 46+ (Fedora 40 or newer) for libadwaita 1.5.

```bash
# Runtime dependencies (system packages, the GObject-introspection way)
sudo dnf install python3-gobject gtk4 libadwaita python3-requests python3-keyring

# Then either run it straight from the source tree:
python3 run.py

# …or install it as a proper command + desktop entry:
pip install --user .
install -Dm644 data/com.idleendeavour.ThingsGNOME.desktop \
  ~/.local/share/applications/com.idleendeavour.ThingsGNOME.desktop
install -Dm644 data/icons/hicolor/scalable/apps/com.idleendeavour.ThingsGNOME.svg \
  ~/.local/share/icons/hicolor/scalable/apps/com.idleendeavour.ThingsGNOME.svg
install -Dm644 data/com.idleendeavour.ThingsGNOME.metainfo.xml \
  ~/.local/share/metainfo/com.idleendeavour.ThingsGNOME.metainfo.xml
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

After `pip install --user .` the command is `things-gnome`, and the app shows up
in your launcher as **Things for GNOME**.

> PyGObject is intentionally **not** a pip dependency — on Fedora you always want
> the distro `python3-gobject` so it matches your system GTK. `pip install .`
> only pulls `requests` and `keyring`.

## Signing in

1. Launch the app and click **Sign in** (or the account button in the header).
2. Enter the **email and password** for your Things Cloud account.
3. It performs the same login the Mac app does, fetches your history, and replays
   it into the views. The first sync downloads everything; later syncs are
   incremental.

Your email is stored in the app's config. Your password is stored in the system
**keyring** (GNOME Keyring / `libsecret`) via the `keyring` package. If no keyring
is available it falls back to a `0600` file in your config dir and tells you it
did so.

Credentials are sent **only** to `cloud.culturedcode.com`, exactly as the Mac
client sends them. Nothing else leaves your machine.

## Loading a Shortcuts export instead

If you'd rather not sign in, the app understands the JSON your existing
"export Things to JSON" Shortcut produces (areas / projects / todos with
checklists). Point the app at the file and it builds the same views. This is also
handy as a fallback if the cloud protocol ever changes.

## Why (mostly) read-only

Things Cloud is an **append-only event log**. Writing means committing new events
against the current head index, and getting a field wrong (or racing a real device
mid-edit) risks corrupting tasks in an account that has no undo. The app defaults
to read-only for exactly that reason, and still ships with only one write
operation: completing / uncompleting a task, behind **Preferences → "Enable
writing (experimental)"** (off by default) and a **Dry run** switch (on by
default, so turning writing on alone still sends nothing). Everything else —
editing, scheduling, creating tasks, repeats — is designed but not yet built;
the full write protocol, what's confirmed against community references, and
what still needs a live capture to confirm safely are documented in
[REVERSE-ENGINEERING.md](REVERSE-ENGINEERING.md).

## Project layout

```
thingsgnome/
  sync/protocol.py   wire constants, field map, the HOW-IT-WORKS docstring
  sync/auth.py       login() -> Account (history-key, head index)
  sync/client.py     ThingsReadClient: paginated fetch + NEW/EDIT replay
  sync/commit.py     ThingsWriteClient: EDIT/NEW commits, dry-run by default
  model.py           Area/Task model, smart-list logic, both data-source builders,
                      write-delta builders (Task.complete_delta() etc.)
  store.py           credentials (keyring) + local sync cache (XDG dirs)
  application.py     Adw.Application, About
  window.py          the whole UI (NavigationSplitView, sync threading, dialogs,
                      the checkbox write path)
tests/
  test_replay.py     replay + model + smart lists + export import  (no network)
  test_pagination.py pagination/replay regression tests (no network)
  test_commit.py     write-delta shapes, date round-trip, dry-run/live commit (no network)
  test_ui_smoke.py   builds and renders every view headlessly under Xvfb
tools/
  diagnose.py        redacted structural dump of your raw history log
  capture.py         mechanized "make one change, diff the tail" capture loop
data/                desktop entry, AppStream metainfo, original icon
run.py               dev launcher
```

## Running the tests

```bash
python3 tests/test_replay.py          # logic, no network, no display
python3 tests/test_pagination.py      # pagination/replay regressions, no network
python3 tests/test_commit.py          # write deltas + commit client, no network
xvfb-run -a python3 tests/test_ui_smoke.py   # builds the UI headlessly
```

## License

GPL-3.0-or-later. The protocol notes build on the community
`disrupted/things-cloud-api`, `nicolai86/things-cloud-sdk`,
`evanpurkhiser/things3-cloud`, and `wbopan/things-cloud-mcp` projects.
