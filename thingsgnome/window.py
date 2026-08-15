"""Main window: native GNOME UI over the Things Cloud sync layer."""

from __future__ import annotations

import threading
from datetime import date

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .model import Database, Kind, Status, Destination, parse_export_text
from .store import Cache, Credentials
from .sync import (
    AuthError,
    CommitError,
    SyncError,
    ThingsReadClient,
    ThingsWriteClient,
    login,
)
from .sync import protocol as P

SMART = [
    ("inbox", "Inbox", "mail-unread-symbolic"),
    ("today", "Today", "starred-symbolic"),
    ("upcoming", "Upcoming", "x-office-calendar-symbolic"),
    ("anytime", "Anytime", "view-list-symbolic"),
    ("someday", "Someday", "weather-few-clouds-symbolic"),
    ("logbook", "Logbook", "emblem-ok-symbolic"),
]


class NavRow(Adw.ActionRow):
    """A sidebar row that remembers which view it points at."""

    def __init__(self, view, **kwargs):
        super().__init__(**kwargs)
        self.view = view


def fmt_date(d: date | None) -> str:
    return d.strftime("%a %-d %b") if d else ""


def esc(s) -> str:
    """Escape text for Adw rows / labels, which parse Pango markup by default.

    Without this, any task whose title or note contains &, < or > (e.g. a URL with
    query params) fails to render and spams 'failed to set text from markup'.
    """
    return GLib.markup_escape_text(s) if s else ""


class ThingsWindow(Adw.ApplicationWindow):
    # Things Cloud has no push notification -- poll for changes made on other
    # devices. Background sync already single-flights against a commit in
    # progress (see start_sync), so this is safe to fire unconditionally.
    PERIODIC_SYNC_SECONDS = 300

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Things for GNOME")
        self.set_default_size(1080, 760)

        self.creds = Credentials()
        self.cache = Cache()
        self.db: Database | None = None
        self.entities, self.head_index = self.cache.load()
        self.current_view = ("smart", "today")
        self.filter = "open"          # open | completed | all
        self.search_text = ""
        self.syncing = False
        # Write-path guards (see REVERSE-ENGINEERING.md "Adding writes"):
        # _rendering is true for the duration of any content rebuild, so a
        # checkbox's initial `active=...` state (or a settings-triggered
        # re-render) can never be mistaken for a user click and fire a commit.
        self._rendering = False
        # committing is a single-flight lock: Things Cloud commits are not
        # optimistic-merge, so two in-flight commits racing the same
        # ancestor-index is a real risk, not just a UI nicety.
        self.committing = False
        # Cached once per render (see _render_content_inner) so _task_row
        # doesn't hit disk + JSON-parse settings for every row.
        self._writes_enabled = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(240)
        self.split.set_max_sidebar_width(320)
        self.toasts.set_child(self.split)

        self._build_sidebar()
        self._build_content()

        # If we have cached data, show it immediately.
        if self.entities:
            self.db = Database.from_entities(self.entities)
            self._render_sidebar()
            self._render_content()

        # Then decide what to do: sync, or prompt for login.
        if self.creds.get_email() and self.creds.get_password():
            self.start_sync()
        elif not self.entities:
            GLib.idle_add(self.show_login)

        # Things Cloud has no push/websocket -- without this, a change made on
        # another device only shows up here after a manual Refresh or an app
        # restart, which reads as "doesn't mirror my phone" even though the
        # data was never wrong, just not re-fetched yet.
        GLib.timeout_add_seconds(self.PERIODIC_SYNC_SECONDS, self._periodic_sync)

    # ===================================================================== #
    # layout scaffolding
    # ===================================================================== #
    def _build_sidebar(self):
        page = Adw.NavigationPage(title="Things")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Refresh", "win.refresh")
        menu.append("Resync from scratch", "win.resync")
        menu.append("Accounts…", "win.accounts")
        menu.append("Preferences", "win.preferences")
        menu.append("About Things for GNOME", "app.about")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)

        self.sync_spinner = Gtk.Spinner()
        header.pack_start(self.sync_spinner)
        toolbar.add_top_bar(header)

        self.sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.sidebar_box.set_margin_top(8)
        self.sidebar_box.set_margin_bottom(12)
        self.sidebar_box.set_margin_start(8)
        self.sidebar_box.set_margin_end(8)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.sidebar_box)
        scroller.set_vexpand(True)
        toolbar.set_content(scroller)

        page.set_child(toolbar)
        self.split.set_sidebar(page)

        # window actions
        self._add_action("refresh", lambda *_: self.start_sync())
        self._add_action("resync", lambda *_: self.resync_from_scratch())
        self._add_action("accounts", lambda *_: self.show_login())
        self._add_action("preferences", lambda *_: self.show_preferences())

    def _add_action(self, name, cb):
        a = Gio.SimpleAction.new(name, None)
        a.connect("activate", cb)
        self.add_action(a)

    def _build_content(self):
        self.content_page = Adw.NavigationPage(title="Today")
        toolbar = Adw.ToolbarView()
        self.content_header = Adw.HeaderBar()

        # filter segmented control
        self.filter_box = Gtk.Box(css_classes=["linked"])
        self._filter_buttons = {}
        first = None
        for key, label in (("open", "Open"), ("completed", "Done"), ("all", "All")):
            btn = Gtk.ToggleButton(label=label)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(key == self.filter)
            btn.connect("toggled", self._on_filter_toggled, key)
            self._filter_buttons[key] = btn
            self.filter_box.append(btn)
        self.content_header.set_title_widget(self.filter_box)

        # search
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic")
        self.search_btn.connect("toggled", self._on_search_toggled)
        self.content_header.pack_end(self.search_btn)
        toolbar.add_top_bar(self.content_header)

        self.search_bar = Gtk.SearchBar()
        self.search_entry = Gtk.SearchEntry(hexpand=True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.search_bar.set_child(self.search_entry)
        self.search_bar.connect_entry(self.search_entry)
        toolbar.add_top_bar(self.search_bar)

        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.content_box.set_margin_top(18)
        self.content_box.set_margin_bottom(36)
        self.content_box.set_margin_start(18)
        self.content_box.set_margin_end(18)
        self.content_clamp = Adw.Clamp(maximum_size=760, child=self.content_box)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(self.content_clamp)
        toolbar.set_content(scroller)

        self.content_page.set_child(toolbar)
        self.split.set_content(self.content_page)

    # ===================================================================== #
    # sidebar
    # ===================================================================== #
    def _render_sidebar(self):
        child = self.sidebar_box.get_first_child()
        while child:
            self.sidebar_box.remove(child)
            child = self.sidebar_box.get_first_child()

        if not self.db:
            return
        lists = self.db.categorize()
        counts = {
            "inbox": lists.inbox, "today": lists.today, "upcoming": lists.upcoming,
            "anytime": lists.anytime, "someday": lists.someday, "logbook": lists.logbook,
        }

        # smart lists
        smart_group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                  css_classes=["navigation-sidebar"])
        for key, label, icon in SMART:
            row = NavRow(("smart", key), title=label)
            row.add_prefix(Gtk.Image(icon_name=icon))
            n = sum(1 for t in counts[key] if (t.is_open or key == "logbook"))
            if n:
                badge = Gtk.Label(label=str(n), css_classes=["dim-label", "caption"])
                row.add_suffix(badge)
            row.set_activatable(True)
            row.connect("activated", self._on_nav_activated)
            smart_group.append(row)
        self.sidebar_box.append(smart_group)

        # areas with their projects
        if self.db.areas or self.db.loose_projects():
            label = Gtk.Label(label="Areas", xalign=0,
                              css_classes=["heading", "dim-label"])
            label.set_margin_start(6)
            self.sidebar_box.append(label)
            area_group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                     css_classes=["boxed-list"])
            for area in self.db.areas:
                exp = Adw.ExpanderRow(title=esc(area.title) or "Untitled area")
                projs = self.db.projects_in_area(area.uuid)
                direct = self.db.tasks_in_area(area.uuid)
                if direct:
                    pr = NavRow(("area", area.uuid), title="All tasks")
                    pr.add_prefix(Gtk.Image(icon_name="view-list-symbolic"))
                    pr.set_activatable(True)
                    pr.connect("activated", self._on_nav_activated)
                    exp.add_row(pr)
                for p in projs:
                    pr = NavRow(("project", p.uuid), title=esc(p.title) or "Untitled")
                    open_n = sum(1 for t in self.db.tasks_in_project(p.uuid) if t.is_open)
                    if open_n:
                        pr.add_suffix(Gtk.Label(label=str(open_n),
                                                css_classes=["dim-label", "caption"]))
                    pr.set_activatable(True)
                    pr.connect("activated", self._on_nav_activated)
                    exp.add_row(pr)
                area_group.append(exp)
            self.sidebar_box.append(area_group)

            loose = self.db.loose_projects()
            if loose:
                lbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                   css_classes=["boxed-list"])
                for p in loose:
                    pr = NavRow(("project", p.uuid), title=esc(p.title) or "Untitled")
                    pr.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
                    pr.set_activatable(True)
                    pr.connect("activated", self._on_nav_activated)
                    lbox.append(pr)
                self.sidebar_box.append(lbox)

    def _on_nav_activated(self, row: NavRow):
        self.current_view = row.view
        if self.search_btn.get_active():
            self.search_btn.set_active(False)
        self._render_content()
        self.split.set_show_content(True)

    # ===================================================================== #
    # content
    # ===================================================================== #
    def _on_filter_toggled(self, btn, key):
        if btn.get_active():
            self.filter = key
            self._render_content()

    def _on_search_toggled(self, btn):
        self.search_bar.set_search_mode(btn.get_active())
        if btn.get_active():
            self.search_entry.grab_focus()
        else:
            self.search_text = ""
            self._render_content()

    def _on_search_changed(self, entry):
        self.search_text = entry.get_text().strip()
        self._render_content()

    def _apply_filter(self, tasks):
        out = tasks
        if self.filter == "open":
            out = [t for t in out if t.is_open]
        elif self.filter == "completed":
            out = [t for t in out if t.is_closed]
        return out

    def _clear_content(self):
        child = self.content_box.get_first_child()
        while child:
            self.content_box.remove(child)
            child = self.content_box.get_first_child()

    def _render_content(self):
        # Guards every checkbox built below: rebuilding rows sets `active=...`
        # on fresh GtkCheckButtons, which must never be mistaken for a user
        # toggle. See the _rendering comment in __init__.
        self._rendering = True
        try:
            self._render_content_inner()
        finally:
            self._rendering = False

    def _render_content_inner(self):
        self._clear_content()
        self._writes_enabled = bool(self.creds.load_settings().get("writes_enabled"))
        if not self.db:
            self._show_status("checkbox-checked-symbolic", "No tasks yet",
                              "Sign in to Things Cloud to sync your tasks.")
            return

        if self.search_text:
            self.content_page.set_title("Search")
            results = self._apply_filter(self.db.search(self.search_text))
            self._render_flat("Results", results, show_project=True)
            if not results:
                self._show_status("system-search-symbolic", "No matches", "")
            return

        kind, ident = self.current_view
        if kind == "smart":
            self._render_smart(ident)
        elif kind == "area":
            self._render_area(ident)
        elif kind == "project":
            self._render_project(ident)

    def _render_smart(self, key):
        lists = self.db.categorize()
        label = dict((k, lbl) for k, lbl, _ in SMART)[key]
        self.content_page.set_title(label)
        pool = getattr(lists, key)
        pool = self._apply_filter(pool) if key != "logbook" else pool
        if not pool:
            self._show_status("emblem-ok-symbolic", "Nothing here", "")
            return
        # group by project for readability
        groups: dict[str | None, list] = {}
        for t in pool:
            groups.setdefault(t.project_id, []).append(t)
        # un-projected first
        if None in groups:
            self._render_flat(None, groups.pop(None), show_project=False)
        for pid, items in groups.items():
            proj = self.db.project(pid)
            self._render_flat(proj.title if proj else "Project", items, show_project=False)

    def _render_area(self, area_id):
        area = self.db.area(area_id)
        if not area:
            return
        self.content_page.set_title(area.title or "Area")
        for proj in self.db.projects_in_area(area_id):
            tasks = self._apply_filter(self.db.tasks_in_project(proj.uuid))
            self._render_flat(proj.title, tasks, show_project=False, project=proj)
        direct = self._apply_filter(self.db.tasks_in_area(area_id))
        if direct:
            self._render_flat(None, direct, show_project=False)

    def _render_project(self, project_id):
        proj = self.db.project(project_id)
        if not proj:
            return
        self.content_page.set_title(proj.title or "Project")
        tasks = self._apply_filter(self.db.tasks_in_project(project_id))
        # group by heading
        groups: dict[str, list] = {}
        for t in tasks:
            groups.setdefault(t.heading_title or "", []).append(t)
        if proj.notes:
            note = Gtk.Label(label=proj.notes, xalign=0, wrap=True,
                             css_classes=["dim-label"])
            self.content_box.append(note)
        # no-heading first
        if "" in groups:
            self._render_flat(None, groups.pop(""), show_project=False)
        for heading, items in groups.items():
            self._render_flat(heading, items, show_project=False)
        if not tasks:
            self._show_status("emblem-ok-symbolic", "No items", "")

    def _render_flat(self, title, tasks, show_project=False, project=None):
        group = Adw.PreferencesGroup()
        if title:
            group.set_title(esc(title))
        if not tasks:
            return
        lb_holder = group
        for t in tasks:
            lb_holder.add(self._task_row(t, show_project=show_project))
        self.content_box.append(group)

    def _task_row(self, t, show_project=False):
        meta = []
        if t.scheduled:
            meta.append("📅 " + fmt_date(t.scheduled))
        if t.deadline:
            meta.append("⚑ " + fmt_date(t.deadline))
        if t.repeat_rule:
            meta.append("🔁 " + t.repeat_rule)
        if t.destination is Destination.SOMEDAY:
            meta.append("Someday")
        if show_project and t.project_id:
            proj = self.db.project(t.project_id)
            if proj:
                meta.append(proj.title)
        for tag in t.tags:
            meta.append("#" + tag)
        subtitle = "   ·   ".join(meta)

        has_expand = bool(t.notes.strip()) or bool(t.checklist)
        if has_expand:
            row = Adw.ExpanderRow(title=esc(t.title) or "(untitled)")
            if subtitle:
                row.set_subtitle(esc(subtitle))
            if t.notes.strip():
                note_row = Adw.ActionRow(subtitle=esc(t.notes.strip()))
                note_row.set_subtitle_lines(0)
                note_row.add_css_class("dim-label")
                row.add_row(note_row)
            for text, done in t.checklist:
                cr = Adw.ActionRow(title=esc(text))
                cb = Gtk.CheckButton(active=done, sensitive=False, valign=Gtk.Align.CENTER)
                cr.add_prefix(cb)
                row.add_row(cr)
        else:
            row = Adw.ActionRow(title=esc(t.title) or "(untitled)")
            if subtitle:
                row.set_subtitle(esc(subtitle))

        # leading status indicator
        if t.status is Status.CANCELED:
            prefix = Gtk.Image(icon_name="window-close-symbolic")
            prefix.set_tooltip_text("Cancelled")
        else:
            writes_on = self._writes_enabled
            # `active` is set here, at construction time, deliberately -- setting
            # it via the constructor kwarg does not emit "toggled". Connecting
            # the handler only AFTER this point (and only when writes are on)
            # means a plain read-only checkbox never has a handler to misfire.
            prefix = Gtk.CheckButton(
                active=t.status is Status.COMPLETED,
                sensitive=writes_on,
                valign=Gtk.Align.CENTER,
            )
            if writes_on:
                prefix.connect("toggled", self._on_task_toggled, t)
            else:
                prefix.set_tooltip_text(
                    "Enable writing in Preferences to complete tasks."
                )
        row.add_prefix(prefix)
        return row

    def _show_status(self, icon, title, description):
        status = Adw.StatusPage(icon_name=icon, title=title)
        if description:
            status.set_description(description)
        status.set_vexpand(True)
        self.content_box.append(status)

    # ===================================================================== #
    # sync
    # ===================================================================== #
    def resync_from_scratch(self):
        """Drop the local cache and re-read the whole history from index 0.

        Use this if a previous sync stored a partial snapshot: a normal refresh
        only fetches events after the cached head, so it can't backfill a gap.
        """
        if self.syncing or self.committing:
            if self.committing:
                self.toasts.add_toast(Adw.Toast(
                    title="A change is still saving — try again in a moment.",
                    timeout=3))
            return
        self.cache.clear()
        self.entities = {}
        self.head_index = 0
        self.start_sync()

    def start_sync(self):
        # Also refuse while a commit is in flight: both _sync_done and
        # _commit_done write self.entities/head_index/cache -- if a sync
        # started before a commit finishes, the sync's pre-commit snapshot can
        # land last and silently erase the commit (the task un-completes, the
        # head moves backwards). It self-heals on the next sync, but a user's
        # very first live click shouldn't hit that.
        if self.syncing or self.committing:
            return
        email = self.creds.get_email()
        password = self.creds.get_password()
        if not (email and password):
            self.show_login()
            return
        self.syncing = True
        self.sync_spinner.start()
        ua = self.creds.load_settings().get("user_agent") or None
        threading.Thread(
            target=self._sync_worker, args=(email, password, ua), daemon=True
        ).start()

    def _periodic_sync(self) -> bool:
        """GLib.timeout_add_seconds callback -- return True to keep repeating."""
        self.start_sync()
        return True

    def _sync_worker(self, email, password, user_agent):
        try:
            account = login(email, password, user_agent=user_agent) if user_agent \
                else login(email, password)
            client = ThingsReadClient(account, user_agent=user_agent)
            # incremental if we have a cache, full otherwise
            start = self.head_index if self.entities else 0
            result = client.replay(start_index=start, entities=dict(self.entities))
            GLib.idle_add(self._sync_done, result.entities, result.head_index, None)
        except (AuthError, SyncError) as e:
            GLib.idle_add(self._sync_done, None, None, str(e))
        except Exception as e:  # pragma: no cover - defensive
            GLib.idle_add(self._sync_done, None, None, f"Unexpected error: {e}")

    def _sync_done(self, entities, head_index, error):
        self.syncing = False
        self.sync_spinner.stop()
        if error:
            toast = Adw.Toast(title=error, timeout=6)
            self.toasts.add_toast(toast)
            if "password" in error.lower() or "expired" in error.lower():
                self.show_login()
            return
        self.entities = entities
        self.head_index = head_index
        self.cache.save(entities, head_index)
        self.db = Database.from_entities(entities)
        self._render_sidebar()
        self._render_content()
        self.toasts.add_toast(Adw.Toast(title="Synced", timeout=2))

    # ===================================================================== #
    # writes (complete / uncomplete a task) -- see REVERSE-ENGINEERING.md
    # ===================================================================== #
    def _revert_checkbox(self, btn, task):
        """Snap a checkbox back to the task's actual status without firing
        another commit -- used on dry-run (nothing was sent) and on error."""
        self._rendering = True
        try:
            btn.set_active(task.status is Status.COMPLETED)
        finally:
            self._rendering = False

    def _on_task_toggled(self, btn, task):
        if self._rendering:
            return

        if self.committing or self.syncing:
            self._revert_checkbox(btn, task)
            self.toasts.add_toast(Adw.Toast(
                title="A change is still in progress — try again in a moment.",
                timeout=3))
            return

        want_complete = btn.get_active()
        delta = task.complete_delta() if want_complete else task.uncomplete_delta()

        settings = self.creds.load_settings()
        dry_run = settings.get("dry_run", True)

        if dry_run:
            import json
            preview = json.dumps({task.uuid: {"t": 1, "e": "Task6", "p": delta}})
            print(f"[dry-run] would commit: {preview}")
            self.toasts.add_toast(Adw.Toast(
                title="Dry run — nothing sent (see terminal output; "
                      "not visible if launched outside a terminal)",
                timeout=5))
            self._revert_checkbox(btn, task)
            return

        email = self.creds.get_email()
        password = self.creds.get_password()
        if not (email and password):
            self._revert_checkbox(btn, task)
            self.show_login()
            return

        self.committing = True
        btn.set_sensitive(False)
        ua = settings.get("user_agent") or None
        threading.Thread(
            target=self._commit_worker,
            args=(email, password, ua, task.uuid, delta, btn, task),
            daemon=True,
        ).start()

    def _commit_worker(self, email, password, user_agent, uuid, delta, btn, task):
        try:
            account = login(email, password, user_agent=user_agent) if user_agent \
                else login(email, password)
            read_client = ThingsReadClient(account, user_agent=user_agent)
            # Read the head fresh immediately before committing (Things Cloud has
            # no optimistic merge) -- this also folds in anything a real device
            # committed since our last sync.
            fresh = read_client.replay(start_index=self.head_index, entities=dict(self.entities))

            write_client = ThingsWriteClient(account, user_agent=user_agent)
            result = write_client.edit(
                uuid, delta, ancestor_index=fresh.head_index, dry_run=False,
            )

            # Apply our own delta locally rather than re-reading: the commit we
            # just made landed at `fresh.head_index` on the server, so a replay
            # starting from `fresh.head_index` (or from the adopted
            # server-head-index) would skip straight past it and the checkbox
            # would appear to silently revert.
            ThingsReadClient._apply(
                fresh.entities, uuid, {"t": P.UPDATE_EDIT, "e": P.ENTITY_TASK, "p": delta},
            )
            GLib.idle_add(
                self._commit_done, fresh.entities, result.server_head_index, None, btn, task,
            )
        except (AuthError, SyncError, CommitError) as e:
            GLib.idle_add(self._commit_done, None, None, str(e), btn, task)
        except Exception as e:  # pragma: no cover - defensive
            GLib.idle_add(self._commit_done, None, None, f"Unexpected error: {e}", btn, task)

    def _commit_done(self, entities, head_index, error, btn, task):
        self.committing = False
        if error:
            self.toasts.add_toast(Adw.Toast(title=error, timeout=6))
            self._revert_checkbox(btn, task)
            btn.set_sensitive(True)
            return
        self.entities = entities
        self.head_index = head_index
        self.cache.save(entities, head_index)
        self.db = Database.from_entities(entities)
        self._render_sidebar()
        self._render_content()
        self.toasts.add_toast(Adw.Toast(title="Saved", timeout=2))

    # ===================================================================== #
    # login dialog
    # ===================================================================== #
    def show_login(self):
        dialog = Adw.Dialog()
        dialog.set_title("Sign in to Things Cloud")
        dialog.set_content_width(420)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            description=(
                "Use your Things Cloud email and password. Credentials are stored in "
                "your system keyring and sent only to cloud.culturedcode.com."
            )
        )
        email_row = Adw.EntryRow(title="Email")
        email_row.set_text(self.creds.get_email() or "")
        pw_row = Adw.PasswordEntryRow(title="Password")
        group.add(email_row)
        group.add(pw_row)
        page.add(group)

        action_group = Adw.PreferencesGroup()
        self._login_error = Gtk.Label(xalign=0, wrap=True, css_classes=["error"])
        self._login_error.set_visible(False)
        action_group.add(self._login_error)

        btn_box = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        self._login_spinner = Gtk.Spinner()
        sign_in = Gtk.Button(label="Sign in", css_classes=["suggested-action"])
        btn_box.append(self._login_spinner)
        btn_box.append(sign_in)
        action_group.add(btn_box)
        page.add(action_group)

        toolbar.set_content(page)
        dialog.set_child(toolbar)

        def attempt(*_):
            if self.committing:
                # _login_ok writes self.entities/head_index/cache same as
                # _commit_done -- letting both land is the same stale-write
                # race as sync-vs-commit (see start_sync).
                self._login_error.set_text(
                    "A change is still saving — try again in a moment.")
                self._login_error.set_visible(True)
                return
            email = email_row.get_text().strip()
            password = pw_row.get_text()
            if not email or not password:
                self._login_error.set_text("Enter both email and password.")
                self._login_error.set_visible(True)
                return
            sign_in.set_sensitive(False)
            self._login_spinner.start()
            self._login_error.set_visible(False)
            ua = self.creds.load_settings().get("user_agent") or None
            threading.Thread(
                target=self._login_worker,
                args=(email, password, ua, dialog, sign_in),
                daemon=True,
            ).start()

        sign_in.connect("clicked", attempt)
        pw_row.connect("entry-activated", attempt)
        dialog.present(self)

    def _login_worker(self, email, password, ua, dialog, button):
        try:
            account = login(email, password, user_agent=ua) if ua else login(email, password)
            client = ThingsReadClient(account, user_agent=ua)
            result = client.replay(start_index=0)
            GLib.idle_add(self._login_ok, email, password,
                          result.entities, result.head_index, dialog)
        except (AuthError, SyncError) as e:
            GLib.idle_add(self._login_fail, str(e), button)
        except Exception as e:  # pragma: no cover
            GLib.idle_add(self._login_fail, f"Unexpected error: {e}", button)

    def _login_ok(self, email, password, entities, head_index, dialog):
        self.creds.save(email, password)
        self.entities = entities
        self.head_index = head_index
        self.cache.save(entities, head_index)
        self.db = Database.from_entities(entities)
        self._login_spinner.stop()
        dialog.close()
        self._render_sidebar()
        self._render_content()
        if self.creds.using_fallback:
            self.toasts.add_toast(Adw.Toast(
                title="Saved to a local file (install 'keyring' for secure storage)",
                timeout=6))
        else:
            self.toasts.add_toast(Adw.Toast(title="Signed in", timeout=2))

    def _login_fail(self, error, button):
        self._login_spinner.stop()
        button.set_sensitive(True)
        self._login_error.set_text(error)
        self._login_error.set_visible(True)

    # ===================================================================== #
    # preferences
    # ===================================================================== #
    def show_preferences(self):
        dialog = Adw.PreferencesDialog()
        page = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")

        account = Adw.PreferencesGroup(title="Account")
        email = self.creds.get_email() or "Not signed in"
        account.add(Adw.ActionRow(title="Email", subtitle=email))
        signout = Adw.ActionRow(title="Sign out",
                                subtitle="Forget credentials and clear the local cache")
        btn = Gtk.Button(label="Sign out", valign=Gtk.Align.CENTER,
                         css_classes=["destructive-action"])
        btn.connect("clicked", lambda *_: self._sign_out(dialog))
        signout.add_suffix(btn)
        account.add(signout)
        page.add(account)

        sync = Adw.PreferencesGroup(
            title="Sync",
            description=(
                "Things Cloud is undocumented and unofficial. If sync stops working, "
                "set the User-Agent to match a current Things for Mac build "
                "(see the project's REVERSE-ENGINEERING guide)."
            ),
        )
        ua_row = Adw.EntryRow(title="User-Agent override")
        ua_row.set_text(self.creds.load_settings().get("user_agent") or "")
        ua_row.connect("changed", self._on_ua_changed)
        sync.add(ua_row)

        wsettings = self.creds.load_settings()
        writes_enabled = bool(wsettings.get("writes_enabled"))
        dry_run = wsettings.get("dry_run", True)

        writes = Adw.SwitchRow(
            title="Enable writing (experimental)",
            subtitle=(
                "Only completing/uncompleting a task, for now. Undocumented "
                "protocol, no undo — read REVERSE-ENGINEERING.md first."
            ),
            active=writes_enabled,
        )
        dry_run_row = Adw.SwitchRow(
            title="Dry run (log only, don't send)",
            subtitle=(
                "Prints the commit to the terminal instead of sending it — "
                "only visible if you launched this app from a terminal. "
                "Off = changes are actually sent to Things Cloud."
            ),
            active=dry_run,
            sensitive=writes_enabled,
        )
        writes.connect("notify::active", self._on_writes_enabled_changed, dry_run_row)
        dry_run_row.connect("notify::active", self._on_dry_run_changed)
        sync.add(writes)
        sync.add(dry_run_row)
        page.add(sync)

        dialog.add(page)
        dialog.present(self)

    def _on_ua_changed(self, row):
        settings = self.creds.load_settings()
        text = row.get_text().strip()
        if text:
            settings["user_agent"] = text
        else:
            settings.pop("user_agent", None)
        self.creds.save_settings(settings)

    def _on_writes_enabled_changed(self, row, _pspec, dry_run_row):
        enabled = row.get_active()
        settings = self.creds.load_settings()
        settings["writes_enabled"] = enabled
        self.creds.save_settings(settings)
        dry_run_row.set_sensitive(enabled)
        self._render_content()

    def _on_dry_run_changed(self, row, _pspec):
        settings = self.creds.load_settings()
        settings["dry_run"] = row.get_active()
        self.creds.save_settings(settings)

    def _sign_out(self, dialog):
        self.creds.clear()
        self.cache.clear()
        self.entities, self.head_index = {}, 0
        self.db = None
        self._render_sidebar()
        self._render_content()
        dialog.close()
        self.show_login()
