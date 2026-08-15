"""Application object (libadwaita)."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from .window import ThingsWindow  # noqa: E402
from .store import APP_ID  # noqa: E402

VERSION = "0.1.0"


class ThingsApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._add_action("quit", lambda *_: self.quit(), accels=["<primary>q"])
        self._add_action("about", self._on_about)

    def _add_action(self, name, cb, accels=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", cb)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = ThingsWindow(application=self)
        win.present()

    def _on_about(self, *_):
        about = Adw.AboutDialog(
            application_name="Things for GNOME",
            application_icon=APP_ID,
            version=VERSION,
            developer_name="IdleEndeavour",
            comments=(
                "A native GNOME client that reads your Things 3 tasks from Things "
                "Cloud. Unofficial and not affiliated with Cultured Code."
            ),
            license_type=Gtk_License_GPL(),
            website="https://idleendeavor.com",
        )
        win = self.props.active_window
        about.present(win)


def Gtk_License_GPL():
    from gi.repository import Gtk

    return Gtk.License.GPL_3_0


def main() -> int:
    app = ThingsApplication()
    return app.run(None)
