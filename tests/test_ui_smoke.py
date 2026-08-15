"""Headless smoke test: construct the real window + render synthetic data.

Catches GTK/Adw API misuse without needing credentials or a real display
(run under xvfb-run). Does NOT start the main loop or hit the network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from thingsgnome.model import Database  # noqa: E402
from tests.test_replay import build_synthetic_state  # noqa: E402


def run():
    Adw.init()
    from thingsgnome.window import ThingsWindow

    app = Adw.Application(application_id="com.idleendeavour.ThingsGNOME.test")

    holder = {}

    def on_activate(a):
        win = ThingsWindow(application=a)
        # inject synthetic data instead of syncing
        win.db = Database.from_entities(build_synthetic_state())
        win._render_sidebar()
        for view in [("smart", "today"), ("smart", "logbook"),
                     ("area", win.db.areas[0].uuid),
                     ("project", win.db.projects[0].uuid)]:
            win.current_view = view
            win._render_content()
        win.filter = "all"
        win._render_content()
        win.search_text = "licence"
        win._render_content()
        holder["ok"] = True
        a.quit()

    app.connect("activate", on_activate)
    app.run([])
    assert holder.get("ok"), "window did not build"
    print("OK: window builds and renders all views")


if __name__ == "__main__":
    run()
