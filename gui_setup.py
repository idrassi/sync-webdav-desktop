"""GTK onboarding and configuration dialog. No privileged work on the UI thread."""
from dataclasses import replace
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from settings import HOME, Settings
from setup_engine import preflight
from session_guidance import AUTO_LOGOUT_HINT, SECURITY_URL


class SetupDialog(Gtk.Dialog):
    def __init__(self, parent, settings: Settings):
        super().__init__(title="Connect to Sync", transient_for=parent, modal=True)
        self.settings = settings
        self.set_default_size(640, 560)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Review setup", Gtk.ResponseType.APPLY)
        self.connect_button = self.add_button("Connect in browser", Gtk.ResponseType.OK)
        self.connect_button.get_style_context().add_class("suggested-action")
        self.connect_button.set_sensitive(False)
        box = self.get_content_area()
        box.set_spacing(12)
        box.set_border_width(20)
        intro = Gtk.Label(xalign=0, wrap=True, label=
            "Set up your encrypted Sync files as a Linux drive. The app installs missing requirements, "
            "downloads Sync's official WebDAV server, authenticates in your browser, configures Rclone, "
            "and creates automatic user services. No terminal commands or password copying needed.")
        intro.set_max_width_chars(75)
        box.pack_start(intro, False, False, 0)
        policy_hint = Gtk.Label(label=AUTO_LOGOUT_HINT, xalign=0, wrap=True)
        policy_hint.set_max_width_chars(75)
        box.pack_start(policy_hint, False, False, 0)
        box.pack_start(Gtk.LinkButton.new_with_label(SECURITY_URL, 'Open Sync account security settings'), False, False, 0)
        grid = Gtk.Grid(column_spacing=16, row_spacing=12)
        self.content = Gtk.ComboBoxText()
        for ident, label in [("files", "Sync Folder"), ("vault", "Vault"), ("folder", "Specific Sync folder")]:
            self.content.append(ident, label)
        self.content.set_active_id(settings.content)
        self.folder = Gtk.Entry(text=settings.folder, placeholder_text="Documents/My folder")
        self.mount = Gtk.Entry(text=settings.mount_point)
        browse = Gtk.Button.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON)
        browse.connect("clicked", self.choose_mount)
        mount_row = Gtk.Box(spacing=6)
        mount_row.pack_start(self.mount, True, True, 0)
        mount_row.pack_start(browse, False, False, 0)
        self.port = Gtk.SpinButton.new_with_range(1024, 65535, 1)
        self.port.set_value(settings.port)
        self.profile = Gtk.Entry(text=settings.profile)
        for row, (label, widget) in enumerate([
                ("Content", self.content), ("Folder path", self.folder),
                ("Local mount folder", mount_row), ("Local WebDAV port", self.port),
                ("Sync profile", self.profile)]):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        self.folder.set_sensitive(settings.content == "folder")
        self.content.connect("changed", lambda *_: self.folder.set_sensitive(self.content.get_active_id() == "folder"))
        box.pack_start(grid, False, False, 0)
        self.start = Gtk.CheckButton(label="Mount automatically when I log in")
        self.start.set_active(settings.start_services)
        self.linger = Gtk.CheckButton(label="Also start before login (requires administrator approval)")
        self.linger.set_active(settings.before_login)
        self.tray = Gtk.CheckButton(label="Start the tray app at login")
        self.tray.set_active(settings.tray_autostart)
        self.bookmark = Gtk.CheckButton(label="Add Sync Drive to Files / Nautilus bookmarks")
        self.bookmark.set_active(settings.bookmark)
        self.keepalive = Gtk.CheckButton(label='Keep connection active with a read-only request')
        self.keepalive.set_active(settings.keepalive_enabled)
        self.keepalive_interval = Gtk.SpinButton.new_with_range(30, 3600, 1)
        self.keepalive_interval.set_value(settings.keepalive_interval_seconds)
        self.keepalive_interval.set_sensitive(settings.keepalive_enabled)
        keepalive_row = Gtk.Box(spacing=8)
        keepalive_row.pack_start(self.keepalive, True, True, 0)
        keepalive_row.pack_start(self.keepalive_interval, False, False, 0)
        keepalive_row.pack_start(Gtk.Label(label='seconds'), False, False, 0)
        self.keepalive.connect('toggled', lambda *_: self.keepalive_interval.set_sensitive(self.keepalive.get_active()))
        self.reset = Gtk.CheckButton(label="Replace local WebDAV credential (only if it is lost or invalid)")
        self.reset.set_tooltip_text("Requires browser sign-in. This invalidates the previous generated password for all other WebDAV clients, not your Sync account password.")
        self.adopt = Gtk.CheckButton(label="Allow this app to adopt / replace existing Sync user services")
        self.adopt.set_active(settings.managed)
        for widget in [self.start, self.linger, self.tray, self.bookmark, self.reset, self.adopt]:
            box.pack_start(widget, False, False, 0)
        box.pack_start(keepalive_row, False, False, 0)
        self.start.connect("toggled", self.start_changed)
        self.linger.set_sensitive(self.start.get_active())
        self.review = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.review.set_max_width_chars(78)
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(95)
        scroller.add(self.review)
        box.pack_start(scroller, True, True, 0)
        self.review.set_text("Click Review setup to check requirements and preview changes.\n"
                             "Sync WebDAV Desktop is independent and is not endorsed by Sync.com.")
        for widget, signal in [(self.content, "changed"), (self.folder, "changed"), (self.mount, "changed"),
                               (self.port, "value-changed"), (self.profile, "changed"), (self.start, "toggled"),
                               (self.linger, "toggled"), (self.reset, "toggled"), (self.adopt, "toggled"),
                               (self.tray, "toggled"), (self.bookmark, "toggled"),
                               (self.keepalive, 'toggled'), (self.keepalive_interval, 'value-changed') ]:
            widget.connect(signal, lambda *_: self.connect_button.set_sensitive(False))
        self.show_all()

    def start_changed(self, *_):
        self.linger.set_sensitive(self.start.get_active())
        if not self.start.get_active():
            self.linger.set_active(False)

    def choose_mount(self, *_):
        dialog = Gtk.FileChooserDialog(title="Choose an empty mount folder", transient_for=self,
                                        action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        dialog.set_current_folder(str(HOME))
        if dialog.run() == Gtk.ResponseType.OK:
            self.mount.set_text(dialog.get_filename())
        dialog.destroy()

    def values(self) -> Settings:
        profile = self.profile.get_text().strip()
        return replace(self.settings, mount_point=self.mount.get_text().strip(),
                       content=self.content.get_active_id(), folder=self.folder.get_text().strip(),
                       port=self.port.get_value_as_int(), profile=profile,
                       rclone_config=self.settings.rclone_config if profile == self.settings.profile else "",
                       start_services=self.start.get_active(), before_login=self.linger.get_active(),
                       tray_autostart=self.tray.get_active(), bookmark=self.bookmark.get_active(),
                       keepalive_enabled=self.keepalive.get_active(),
                       keepalive_interval_seconds=self.keepalive_interval.get_value_as_int()).validate()

    def review_setup(self):
        try:
            values = self.values()
            report = preflight(values)
            errors = list(report.errors)
            if report.legacy and not self.adopt.get_active():
                errors.append("Approve adoption of your existing services, or cancel to keep them unchanged.")
            lines = [f"Content: {values.content}; mount: {values.mount_point}; local port: {values.port}",
                     "Local-only WebDAV access; generated credential saved in a private Rclone file.",
                     "Missing packages: " + (", ".join(report.missing) or "none")]
            if self.reset.get_active():
                lines.append("WARNING: Replacing the local credential invalidates it for other WebDAV clients.")
            lines.append(f'Read-only keep-alive: every {values.keepalive_interval_seconds} seconds while the app is open and connected.'
                         if values.keepalive_enabled else 'Read-only keep-alive: disabled.')
            lines.append(AUTO_LOGOUT_HINT)
            lines.extend(report.warnings)
            lines.extend("ERROR: " + error for error in errors)
            self.review.set_text("\n\n".join(lines))
            self.connect_button.set_sensitive(not errors)
        except (ValueError, OSError, RuntimeError) as exc:
            self.review.set_text(str(exc))
            self.connect_button.set_sensitive(False)

    def get_plan(self):
        while True:
            response = self.run()
            if response == Gtk.ResponseType.APPLY:
                self.review_setup()
            elif response == Gtk.ResponseType.OK:
                # Revalidate after the user reviews, because runtime conditions
                # may have changed while the dialog was open.
                self.review_setup()
                if self.connect_button.get_sensitive():
                    return self.values(), self.reset.get_active(), self.adopt.get_active()
            else:
                return None
