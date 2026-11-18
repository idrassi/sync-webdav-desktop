#!/usr/bin/python3
"""GTK/AppIndicator status app for the existing Sync WebDAV + Rclone mount."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import sys
import threading
import time

from dataclasses import replace
from settings import APP_NAME, VERSION, Settings, load_settings, save_settings, settings_path
from ui_policy import tray_state, connection_notice, TRAY_ENTRY_KEYS, PAGES, WebDAVTimeoutGrace
from keepalive import KeepAliveResult, KeepAliveSchedule, ping
from session_guidance import (AUTO_LOGOUT_HINT, AUTO_LOGOUT_HELP, KEEPALIVE_POLICY_NOTE,
                              SECURITY_URL)
from backend import (APP_DIR, APP_ID, Snapshot, autostart_path, collect_snapshot,
                     redact, set_autostart)

LOG = logging.getLogger(__name__)
EXPIRY_RETRY_SECONDS = 60


class GuiUnavailable(RuntimeError):
    pass


def run_gui(settings: Settings) -> int:
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as Indicator, Gio, GLib, Gtk
    except (ImportError, ValueError) as exc:
        raise GuiUnavailable(str(exc)) from exc

    class Application(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
            self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="sync-monitor")
            self.snapshot: Snapshot | None = None
            self.display_snapshot: Snapshot | None = None
            self.timeout_grace = WebDAVTimeoutGrace()
            self.refreshing = False
            self.closing = False
            self.busy = ""
            self.previous_state = None
            self.smoke = False
            self.smoke_errors = []
            self.smoke_exit_code = 0
            self.settings = settings
            self.cancel_event = None
            self.cancel_enabled = False
            self.operation_retry = None
            self.repaint_requested = False
            self.keepalive_schedule = KeepAliveSchedule()
            self.keepalive_schedule.configure(self.settings)
            self.keepalive_cancel = None
            self.keepalive_future = None
            self.observed_expiry = None
            self.expiry_retry_key = None
            self.expiry_retry_at = 0.0
            self.history_future = None

        def do_startup(self):
            Gtk.Application.do_startup(self)
            self.hold()  # Closing the window must not terminate the tray monitor.
            self.build_window()
            self.build_menu()
            self.indicator = Indicator.Indicator.new(
                APP_ID, str(APP_DIR / 'icons/sync-webdav-working.png'), Indicator.IndicatorCategory.APPLICATION_STATUS)
            from backend import private_runtime_dir
            from tray_icons import TrayIconPresenter
            self.tray_icons = TrayIconPresenter(self.indicator, APP_DIR / 'icons', private_runtime_dir(),
                                                 GLib.timeout_add, GLib.source_remove)
            self.tray_icons.update('working', 'Checking Sync drive')
            self.indicator.set_title(APP_NAME)
            self.indicator.set_menu(self.menu)
            self.indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
            self.indicator.connect('connection-changed', self.indicator_connection_changed)
            self.refresh()
            self.timer = GLib.timeout_add_seconds(5, self.tick)

        def indicator_connection_changed(self, _indicator, connected):
            if connected and not self.closing:
                self.tray_icons.update(self.display_state if hasattr(self, 'display_state') else 'working',
                                       self.display_title if hasattr(self, 'display_title') else 'Checking Sync drive', force=True)

        def do_command_line(self, command_line):
            arguments = command_line.get_arguments()[1:]
            if "--smoke-test" in arguments:
                self.smoke = True
                self.show_page('preferences')
                self.smoke_first_navigation = self.stack.get_visible_child_name() == 'preferences'
                GLib.timeout_add_seconds(4, self.finish_smoke_test)
            elif '--tray-icon-test' in arguments:
                self.smoke = True  # Suppress notifications for presentation-only test.
                self.run_tray_icon_test()
            elif "--tray" not in arguments:
                self.activate()
            return 0

        def run_tray_icon_test(self):
            # Exercise only the display pipeline, not services/authentication.
            # The regular monitor restores real status after the short sequence.
            self.icon_test_active = True
            sequence = [('working', 'Tray icon test: amber'), ('online', 'Tray icon test: green'),
                        ('error', 'Tray icon test: red'), ('online', 'Tray icon test: green')]
            for index, (state, title) in enumerate(sequence):
                def present(state=state, title=title):
                    self.tray_icons.update(state, title)
                    self.summary_item.set_label(title)
                    return False
                GLib.timeout_add(index * 3000 + 1, present)
            def finish():
                self.icon_test_active = False
                self.render()
                self.smoke = False
                return False
            GLib.timeout_add(len(sequence) * 3000, finish)

        def do_activate(self):
            self.show_page('overview')

        def show_page(self, page):
            if page not in PAGES:
                return
            # Gtk.Stack cannot select a child that has not yet been shown.
            # First tray navigation must open the requested page, not Overview.
            self.window.show_all()
            self.stack.set_visible_child_name(page)
            self.window.present()
            if self.snapshot:
                self.render()
            self.refresh()

        def connection_action(self, *_):
            snapshot = self.display_snapshot
            policy = tray_state(snapshot, bool(self.busy) or bool(snapshot and snapshot.recovery_busy),
                                bool(self.cancel_event) and self.cancel_enabled)
            if not policy.action_enabled:
                return
            handlers = {'setup': self.setup, 'restart': self.restart, 'restore': self.restore,
                        'disconnect': self.disconnect, 'cancel': self.cancel_operation}
            if policy.action in handlers:
                handlers[policy.action]()

        def build_window(self):
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title(APP_NAME)
            self.window.set_default_size(760, 650)
            self.window.set_icon_name(APP_ID)
            self.window.connect('delete-event', self.hide_window)
            header = Gtk.HeaderBar(title=APP_NAME, show_close_button=True)
            self.window.set_titlebar(header)
            refresh = Gtk.Button.new_from_icon_name('view-refresh-symbolic', Gtk.IconSize.BUTTON)
            refresh.set_tooltip_text('Refresh connection status')
            refresh.connect('clicked', lambda *_: self.refresh(repaint=True))
            header.pack_end(refresh)

            shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            self.window.add(shell)
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
            switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER, margin_top=14)
            shell.pack_start(switcher, False, False, 0)
            shell.pack_start(self.stack, True, True, 0)
            self.message_label = Gtk.Label(xalign=0, wrap=True, margin_start=20, margin_end=20, margin_bottom=14)
            self.message_label.set_max_width_chars(78)
            shell.pack_end(self.message_label, False, False, 0)

            def page(name, title):
                content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin=20)
                scroll = Gtk.ScrolledWindow()
                scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                scroll.add(content)
                self.stack.add_titled(scroll, name, title)
                return content

            def text(content, value, dim=False):
                label = Gtk.Label(label=value, xalign=0, wrap=True)
                label.set_max_width_chars(78)
                if dim: label.get_style_context().add_class('dim-label')
                content.pack_start(label, False, False, 0)
                return label

            def heading(content, value):
                label = Gtk.Label(xalign=0)
                label.set_markup('<b>' + GLib.markup_escape_text(value) + '</b>')
                content.pack_start(label, False, False, 0)

            def button(content, label, callback):
                widget = Gtk.Button(label=label, halign=Gtk.Align.START)
                widget.connect('clicked', callback)
                content.pack_start(widget, False, False, 0)
                return widget

            overview = page('overview', 'Overview')
            row = Gtk.Box(spacing=16, margin_top=12, margin_bottom=8)
            self.status_icon = Gtk.Image.new_from_file(str(APP_DIR / 'icons/sync-webdav-working.svg'))
            row.pack_start(self.status_icon, False, False, 0)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.title_label = Gtk.Label(xalign=0, wrap=True)
            self.title_label.set_markup('<big><b>Checking Sync drive…</b></big>')
            self.detail_label = Gtk.Label(xalign=0, wrap=True)
            self.detail_label.set_max_width_chars(65)
            labels.pack_start(self.title_label, False, False, 0)
            labels.pack_start(self.detail_label, False, False, 0)
            row.pack_start(labels, True, True, 0)
            overview.pack_start(row, False, False, 0)
            self.security_link = Gtk.LinkButton.new_with_label(SECURITY_URL, 'Review Sync Auto logout settings')
            self.security_link.set_halign(Gtk.Align.START)
            self.security_link.set_no_show_all(True)
            self.security_link.connect('activate-link', self.open_security_settings)
            overview.pack_start(self.security_link, False, False, 0)
            heading(overview, 'Your drive')
            self.path_label = text(overview, self.settings.mount_point, True)
            self.path_label.set_selectable(True)
            actions = Gtk.Box(spacing=10)
            self.open_button = Gtk.Button(label='Open drive')
            self.open_button.get_style_context().add_class('suggested-action')
            self.open_button.connect('clicked', self.open_drive)
            self.connection_button = Gtk.Button(label='Connect')
            self.connection_button.connect('clicked', self.connection_action)
            actions.pack_start(self.open_button, False, False, 0)
            actions.pack_start(self.connection_button, False, False, 0)
            overview.pack_start(actions, False, False, 0)
            self.overview_keepalive = text(overview, 'Checking keep-alive…', True)
            text(overview, 'Closing this window keeps the tray app running. Quitting the app leaves the drive mounted.', True)
            button(overview, 'View connection diagnostics', lambda *_: self.show_page('diagnostics'))

            preferences = page('preferences', 'Preferences')
            heading(preferences, 'Connection')
            text(preferences, 'Choose Sync Folder or Vault, the mount location, and drive startup options.')
            self.setup_button = button(preferences, 'Configure connection…', self.setup)
            heading(preferences, 'Application startup')
            self.autostart_item = Gtk.CheckButton(label='Start tray app when I log in')
            self.autostart_item.set_active(autostart_path().exists())
            self.autostart_handler = self.autostart_item.connect('toggled', self.toggle_autostart)
            preferences.pack_start(self.autostart_item, False, False, 0)
            text(preferences, 'This controls the tray app only. Drive startup is configured separately.', True)
            heading(preferences, 'Read-only keep-alive')
            self.keepalive_item = Gtk.CheckButton(label='Enable periodic read-only access')
            self.keepalive_item.set_active(self.settings.keepalive_enabled)
            self.keepalive_handler = self.keepalive_item.connect('toggled', self.toggle_keepalive)
            preferences.pack_start(self.keepalive_item, False, False, 0)
            interval_row = Gtk.Box(spacing=10)
            interval_row.pack_start(Gtk.Label(label='Interval (seconds)', xalign=0), False, False, 0)
            self.keepalive_interval = Gtk.SpinButton.new_with_range(30, 3600, 1)
            self.keepalive_interval.set_value(self.settings.keepalive_interval_seconds)
            interval_row.pack_start(self.keepalive_interval, False, False, 0)
            self.keepalive_settings_item = Gtk.Button(label='Save interval')
            self.keepalive_settings_item.connect('clicked', self.save_interval)
            interval_row.pack_start(self.keepalive_settings_item, False, False, 0)
            preferences.pack_start(interval_row, False, False, 0)
            text(preferences, KEEPALIVE_POLICY_NOTE, True)
            heading(preferences, 'Maintenance')
            self.update_item = button(preferences, 'Update WebDAV server…', self.update)
            self.remove_item = button(preferences, 'Remove automatic mount setup…', self.remove_setup)
            self.remove_item.get_style_context().add_class('destructive-action')
            text(preferences, 'Maintenance asks for confirmation. Removing setup keeps cloud files, login and local credentials.', True)

            diagnostics = page('diagnostics', 'Diagnostics')
            heading(diagnostics, 'Connection details')
            grid = Gtk.Grid(column_spacing=18, row_spacing=8)
            self.fields = {}
            for i, name in enumerate(['WebDAV service', 'Rclone service', 'Local server', 'Drive mount', 'Authentication', 'Failure logging', 'Keep-alive', 'Last checked']):
                key = Gtk.Label(label=name, xalign=0)
                key.get_style_context().add_class('dim-label')
                value = Gtk.Label(label='Checking…', xalign=0, selectable=True, wrap=True)
                value.set_max_width_chars(55)
                grid.attach(key, 0, i, 1, 1)
                grid.attach(value, 1, i, 1, 1)
                self.fields[name] = value
            diagnostics.pack_start(grid, False, False, 0)
            diagnostic_actions = Gtk.Box(spacing=8)
            self.restart_button = Gtk.Button(label='Restart services…')
            self.restart_button.connect('clicked', self.restart)
            self.restore_button = Gtk.Button(label='Sign in & restore…')
            self.restore_button.connect('clicked', self.restore)
            self.cancel_button = Gtk.Button(label='Cancel operation')
            self.cancel_button.connect('clicked', self.cancel_operation)
            self.cancel_button.set_no_show_all(True)
            for widget in (self.restart_button, self.restore_button, self.cancel_button):
                diagnostic_actions.pack_start(widget, False, False, 0)
            diagnostics.pack_start(diagnostic_actions, False, False, 0)
            self.logging_item = button(diagnostics, 'Enable private failure logging…', self.enable_logging)
            button(diagnostics, 'Session & keep-alive history…', self.show_activity_history)
            heading(diagnostics, 'Recent diagnostics')
            text(diagnostics, 'Credential masking is applied, but logs may contain folder names. Inspect before sharing.', True)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(220)
            self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                         wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8, right_margin=8, top_margin=8, bottom_margin=8)
            scroller.add(self.log_view)
            diagnostics.pack_start(scroller, True, True, 0)

            help_page = page('help', 'Help')
            heading(help_page, 'Session expiry & account security')
            text(help_page, AUTO_LOGOUT_HINT)
            text(help_page, AUTO_LOGOUT_HELP, True)
            button(help_page, 'Open Sync account security', self.open_security_settings)
            button(help_page, 'Why does my session expire?…', self.session_expiry_help)
            heading(help_page, 'Using the app')
            text(help_page, 'Use Overview for everyday connection actions. Preferences contains startup, keep-alive and maintenance settings. Diagnostics contains recovery controls, logs and activity history.')
            text(help_page, 'Finish file transfers before restarting, signing in, disconnecting or removing setup. Browser authentication is interactive; this app does not change account security policy.', True)
            button(help_page, 'About Sync WebDAV Desktop', self.about)
            self.stack.set_visible_child_name('overview')

        def build_menu(self):
            self.menu = Gtk.Menu()
            def item(label, callback=None):
                widget = Gtk.MenuItem(label=label)
                if callback: widget.connect('activate', callback)
                self.menu.append(widget)
                return widget
            self.summary_item = item('Checking connection…')
            self.summary_item.set_sensitive(False)
            self.menu.append(Gtk.SeparatorMenuItem())
            item('Open Sync WebDAV', lambda *_: self.show_page('overview'))
            self.open_item = item('Open drive', self.open_drive)
            self.connection_item = item('Connect', self.connection_action)
            self.menu.append(Gtk.SeparatorMenuItem())
            item('Preferences…', lambda *_: self.show_page('preferences'))
            self.menu.append(Gtk.SeparatorMenuItem())
            self.quit_item = item('Quit app', self.quit_monitor)
            self.menu.show_all()

        def hide_window(self, *_):
            self.window.hide()
            return True

        def notify(self, title, body):
            if not self.smoke:
                notification = Gio.Notification.new(title)
                notification.set_body(body)
                notification.set_icon(Gio.ThemedIcon.new(APP_ID))
                self.send_notification("sync-status", notification)

        def set_message(self, message):
            self.message_label.set_text(redact(message))

        def refresh(self, repaint=False):
            self.repaint_requested = self.repaint_requested or repaint
            if self.closing or self.refreshing:
                return
            future = self.pool.submit(collect_snapshot, self.settings)
            self.refreshing = True
            future.add_done_callback(lambda result: GLib.idle_add(self.refreshed, result))

        def refreshed(self, future):
            self.refreshing = False
            if self.closing:
                return False
            try:
                self.snapshot = future.result()
                self.display_snapshot = self.timeout_grace.update(self.snapshot, bool(self.busy))
                expiry_key = (self.snapshot.session_issued_at, self.snapshot.webdav.active_exit_monotonic)
                if (self.snapshot.auth_reason == 'expired' and self.snapshot.session_issued_at and not self.smoke
                        and self.observed_expiry != expiry_key and self.history_future is None
                        and (self.expiry_retry_key != expiry_key or time.monotonic() >= self.expiry_retry_at)):
                    from activity_history import observe_expiry
                    self.history_future = self.pool.submit(observe_expiry, self.settings, self.snapshot)
                    self.history_future.add_done_callback(lambda result: GLib.idle_add(self.expiry_recorded, expiry_key, result))
                repaint = self.repaint_requested
                self.repaint_requested = False
                self.render(force_icon=repaint)
            except Exception as exc:
                self.set_message("Could not check status: " + str(exc))
                self.smoke_errors.append(redact(str(exc)))
            return False

        def expiry_recorded(self, key, future):
            self.history_future = None
            if not self.closing:
                try:
                    future.result()
                    self.observed_expiry = key
                    self.expiry_retry_key = None
                except (OSError, ValueError):
                    first_failure = self.expiry_retry_key != key
                    self.expiry_retry_key = key
                    self.expiry_retry_at = time.monotonic() + EXPIRY_RETRY_SECONDS
                    if first_failure:
                        self.set_message('Could not record session-expiry history; check private history permissions.')
            return False

        def render(self, force_icon=False):
            snapshot = self.display_snapshot
            if snapshot is None:
                return
            busy = self.busy or ("Recovery is running in another window" if snapshot.recovery_busy else "")
            state = "working" if busy else snapshot.state
            icon = "sync-webdav-" + ("online" if state == "online" else "working" if state == "working" else "error")
            policy = tray_state(snapshot, bool(busy), bool(self.cancel_event) and self.cancel_enabled)
            title = busy or snapshot.title
            detail = "Wait for the operation to finish; authenticate in your browser if requested." if busy else snapshot.detail
            self.display_state, self.display_title = state, title
            if not getattr(self, 'icon_test_active', False):
                self.tray_icons.update(state, title, force=force_icon)
                self.summary_item.set_label(policy.summary)
            self.connection_item.set_label(policy.action_label)
            self.connection_item.set_sensitive(policy.action_enabled)
            self.connection_button.set_label(policy.action_label)
            self.connection_button.set_sensitive(policy.action_enabled)
            self.connection_button.get_style_context().remove_class('suggested-action')
            self.open_button.get_style_context().remove_class('suggested-action')
            (self.open_button if policy.open_enabled else self.connection_button).get_style_context().add_class('suggested-action')
            self.quit_item.set_sensitive(policy.quit_enabled)
            self.status_icon.set_from_file(str(APP_DIR / f"icons/{icon}.svg"))
            self.title_label.set_markup("<big><b>" + GLib.markup_escape_text(title) + "</b></big>")
            self.detail_label.set_text(detail)
            self.security_link.set_visible(snapshot.auth_reason == 'expired' and not busy)
            self.fields["WebDAV service"].set_text(snapshot.webdav.description)
            self.fields["Rclone service"].set_text(snapshot.rclone.description)
            if snapshot.http_status:
                response = f"HTTP {snapshot.http_status} — responding"
            else:
                problem = 'Probe timed out' if snapshot.http_timed_out else 'Not responding'
                response = f"{problem} on 127.0.0.1:{snapshot.port}"
            self.path_label.set_text(snapshot.mount_point)
            self.fields["Local server"].set_text(response)
            self.fields["Drive mount"].set_text(snapshot.mount_type + (" — STALE (Rclone stopped)" if snapshot.stale_mount else ""))
            auth_text = snapshot.auth_reason or "No authentication failure reported"
            if snapshot.latest_auth_reason and snapshot.latest_auth_reason != snapshot.auth_reason:
                auth_text += f" (latest retry: {snapshot.latest_auth_reason})"
            self.fields["Authentication"].set_text(auth_text)
            self.fields["Failure logging"].set_text("Private log enabled" if snapshot.failure_logging else "Not enabled (legacy daemon output is discarded)")
            # Keep-alive requires an actual successful probe, not the tray's last known state.
            keepalive_text = self.keepalive_schedule.description(self.snapshot, bool(busy))
            if not busy and snapshot.webdav_delayed and self.settings.keepalive_enabled:
                keepalive_text = 'Paused while checking WebDAV'
            self.fields['Keep-alive'].set_text(keepalive_text)
            self.overview_keepalive.set_text('Keep-alive: ' + keepalive_text)
            self.fields["Last checked"].set_text(snapshot.checked_at)
            buffer = self.log_view.get_buffer()
            start, end = buffer.get_bounds()
            display_logs = snapshot.session_evidence + '\n\n' + snapshot.keepalive_history + '\n\nService / daemon diagnostics:\n' + snapshot.logs
            if buffer.get_text(start, end, True) != display_logs:
                buffer.set_text(display_logs)
            for widget in (self.open_item, self.open_button):
                widget.set_sensitive(snapshot.state == "online" and not busy)
            for widget in (self.restart_button, self.restore_button, self.update_item):
                widget.set_sensitive(snapshot.configured and not busy)
            self.setup_button.set_sensitive(not busy)
            self.setup_button.set_label('Configure connection…' if snapshot.configured else 'Set up Sync drive…')
            self.remove_item.set_sensitive(snapshot.managed and not busy)
            self.logging_item.set_sensitive(snapshot.configured and not snapshot.failure_logging and not busy)
            self.keepalive_item.set_sensitive(not busy)
            self.keepalive_settings_item.set_sensitive(not busy and self.settings.keepalive_enabled)
            self.keepalive_interval.set_sensitive(not busy and self.settings.keepalive_enabled)
            self.autostart_item.set_sensitive(not busy)
            self.cancel_button.set_visible(bool(self.cancel_event) and self.cancel_enabled)
            if not busy:
                notice = connection_notice(self.previous_state, snapshot)
                if notice:
                    self.notify(*notice)
                self.previous_state = snapshot.state

        def tick(self):
            if self.closing:
                return False
            try:
                self.refresh()
                self.keepalive_tick()
            except Exception as exc:
                # An exception escaping a GLib timeout removes the source.
                LOG.warning("Monitor tick failed; will retry: %s", redact(str(exc)))
            return True

        def keepalive_tick(self):
            if self.closing or self.smoke or self.keepalive_future is not None:
                return
            generation = self.keepalive_schedule.claim(self.snapshot, bool(self.busy) or
                                                        bool(self.snapshot and self.snapshot.recovery_busy))
            if generation is None:
                return
            cancel = threading.Event()
            try:
                self.keepalive_future = self.pool.submit(ping, self.settings, cancel)
            except Exception:
                self.keepalive_schedule.pause()
                raise
            self.keepalive_cancel = cancel
            self.keepalive_future.add_done_callback(lambda result: GLib.idle_add(self.keepalive_finished, generation, result))
            self.render()

        def keepalive_finished(self, generation, future):
            self.keepalive_future = None
            self.keepalive_cancel = None
            if self.closing:
                return False
            try:
                result = future.result()
            except Exception:
                result = KeepAliveResult('failed', 'Keep-alive could not complete', 'unknown')
            try:
                self.keepalive_schedule.complete(generation, result)
                self.render()
            except Exception as exc:
                LOG.warning("Could not update keep-alive display: %s", redact(str(exc)))
            try:
                self.refresh()
            except Exception as exc:
                LOG.warning("Could not queue a status refresh: %s", redact(str(exc)))
            return False

        def pause_keepalive(self):
            if self.keepalive_cancel:
                self.keepalive_cancel.set()
            self.keepalive_schedule.pause()

        def save_keepalive(self, enabled, seconds):
            if self.busy:
                raise ValueError('Finish the connection operation before changing keep-alive settings')
            updated = replace(self.settings, keepalive_enabled=enabled, keepalive_interval_seconds=seconds).validate()
            save_settings(updated)
            self.pause_keepalive()
            self.settings = updated
            self.keepalive_schedule.configure(updated)
            self.keepalive_interval.set_value(seconds)
            self.keepalive_item.handler_block(self.keepalive_handler)
            self.keepalive_item.set_active(enabled)
            self.keepalive_item.handler_unblock(self.keepalive_handler)
            self.set_message('Keep-alive settings saved. No services were restarted; reads resume only when connected.')
            self.render()

        def save_interval(self, *_):
            try:
                self.save_keepalive(self.keepalive_item.get_active(), self.keepalive_interval.get_value_as_int())
            except (ValueError, OSError) as exc:
                self.set_message('Could not save keep-alive interval: ' + str(exc))

        def toggle_keepalive(self, item):
            try:
                self.save_keepalive(item.get_active(), self.settings.keepalive_interval_seconds)
            except (ValueError, OSError) as exc:
                item.handler_block(self.keepalive_handler)
                item.set_active(self.settings.keepalive_enabled)
                item.handler_unblock(self.keepalive_handler)
                self.set_message('Could not save keep-alive preference: ' + str(exc))

        def show_activity_history(self, *_):
            dialog = Gtk.Dialog(title='Session & keep-alive history', transient_for=self.window, modal=True)
            dialog.set_default_size(740, 500)
            dialog.add_button('Close', Gtk.ResponseType.CLOSE)
            content = dialog.get_content_area()
            content.set_border_width(16)
            view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8, right_margin=8)
            text = (self.snapshot.session_evidence + '\n\n' + self.snapshot.keepalive_history) if self.snapshot else 'Waiting for status and evidence…'
            view.get_buffer().set_text(text)
            scroller = Gtk.ScrolledWindow()
            scroller.set_hexpand(True);scroller.set_vexpand(True)
            scroller.add(view)
            content.pack_start(scroller, True, True, 0)
            content.pack_start(Gtk.Label(label='Evidence contains only timestamps, read result codes and timing. No filenames, tokens or CLI output.\n'
                                               'History is bounded and records only observed reads; activity before collection began is unavailable.',
                                        wrap=True, xalign=0), False, False, 12)
            dialog.show_all();dialog.run();dialog.destroy()

        def confirm(self, title, text):
            self.window.show_all()
            if self.snapshot:
                self.render()
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                       message_type=Gtk.MessageType.WARNING,
                                       buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
            dialog.format_secondary_text(text)
            response = dialog.run()
            dialog.destroy()
            return response == Gtk.ResponseType.OK

        def available(self):
            if self.busy or (self.snapshot and self.snapshot.recovery_busy):
                self.set_message("A recovery operation is already running. Finish or cancel it before retrying.")
                return False
            return True

        def restart(self, *_):
            if not self.available() or not self.confirm("Restart Sync services?",
                    "This temporarily unmounts Sync. Finish file transfers and close files on the drive first."):
                return
            from setup_engine import restart
            self.start_operation("Restarting Sync services…", lambda cancel, progress, lazy:
                                 restart(self.settings, cancel, progress, lazy), cancellable=True)

        def restore(self, *_):
            if not self.available() or not self.confirm("Sign in and restore Sync?",
                    "The app will stop both services, open your browser for sign-in, then start and verify the mount. "
                    "Finish transfers first. Cancelling leaves services stopped until you retry."):
                return
            from setup_engine import restore
            self.start_operation("Signing in and restoring Sync…", lambda cancel, progress, lazy:
                                 restore(self.settings, cancel, progress, lazy), cancellable=True)

        def open_drive(self, *_):
            if not self.display_snapshot or self.display_snapshot.state != "online" or self.busy:
                self.set_message("The drive is not connected. Opening an unmounted folder could lead to saving files locally.")
                return
            try:
                Gio.AppInfo.launch_default_for_uri(Path(self.snapshot.mount_point).as_uri(), None)
            except GLib.Error as exc:
                self.set_message("Could not open Files: " + str(exc))

        def setup(self, *_):
            if not self.available():
                return
            from gui_setup import SetupDialog
            dialog = SetupDialog(self.window, self.settings)
            plan = dialog.get_plan()
            dialog.destroy()
            if plan is None:
                return
            settings, reset, adopt = plan
            from setup_engine import connect
            self.start_operation("Setting up Sync…", lambda cancel, progress, lazy:
                                 connect(settings, cancel, progress, reset, adopt, lazy), cancellable=True)

        def start_operation(self, title, function, cancellable=False, allow_lazy=False):
            self.pause_keepalive()
            self.timeout_grace.reset()
            self.busy = title
            self.cancel_event = threading.Event()
            self.cancel_enabled = cancellable
            self.operation_retry = (title, function, cancellable, allow_lazy)
            self.set_message("")
            self.show_page('overview')
            self.render()
            def progress(message):
                GLib.idle_add(self.operation_progress, message)
            previous_ping = self.keepalive_future
            operation_cancel = self.cancel_event
            def execute():
                # Recovery waits for cancellation/release of a shared read lock;
                # never race an active keep-alive against stop/sign-in/mount.
                if previous_ping:
                    try:
                        previous_ping.result(timeout=35)
                    except TimeoutError:
                        raise RuntimeError('Keep-alive is still stopping. Recovery was not started; wait briefly and retry.') from None
                    except Exception:
                        pass
                return function(operation_cancel, progress, allow_lazy)
            future = self.pool.submit(execute)
            future.add_done_callback(lambda result: GLib.idle_add(self.operation_finished, result))

        def operation_progress(self, message):
            if not self.closing and self.cancel_event:
                self.set_message(message)
            return False

        def operation_finished(self, future):
            if self.closing:
                return False
            self.busy = ""
            retry = self.operation_retry
            self.operation_retry = None
            self.cancel_event = None
            self.cancel_enabled = False
            self.cancel_button.hide()
            try:
                result = future.result()
                self.settings = result if isinstance(result, Settings) else load_settings()
                self.keepalive_schedule.configure(self.settings)
                self.keepalive_interval.set_value(self.settings.keepalive_interval_seconds)
                self.keepalive_item.handler_block(self.keepalive_handler)
                self.keepalive_item.set_active(self.settings.keepalive_enabled)
                self.keepalive_item.handler_unblock(self.keepalive_handler)
                # Connect supplies its own completion message, including any
                # non-fatal desktop integration warnings.
                if not isinstance(result, Settings):
                    self.set_message("Operation completed. Configuration and connection status have been refreshed.")
                self.notify("Sync operation completed", "Open the tray status window to see the current connection.")
                self.autostart_item.handler_block(self.autostart_handler)
                self.autostart_item.set_active(autostart_path().exists())
                self.autostart_item.handler_unblock(self.autostart_handler)
            except Exception as exc:
                from mount_safety import StaleMountBusy
                if isinstance(exc, StaleMountBusy) and retry and not retry[3]:
                    self.set_message(str(exc))
                    self.refresh()
                    self.render()
                    if self.confirm("The orphaned Sync mount is still busy",
                            "Close Nautilus / Files windows and file handles on the drive first. Normal unmount failed. "
                            "Allow lazy detachment of this stale mount and retry? This is permitted only after verifying "
                            "that the exact Sync mount belongs to you and no Rclone owner is running. "
                            "It does not force-unmount a live filesystem or kill processes. Existing handles will stop working. "
                            "Cancel leaves the mount untouched."):
                        self.start_operation(retry[0], retry[1], retry[2], allow_lazy=True)
                    return False
                self.set_message("Operation did not complete: " + str(exc))
                self.notify("Sync needs attention", "Open the status window for the operation result.")
                try:
                    self.settings = load_settings()
                    self.keepalive_schedule.configure(self.settings)
                    self.keepalive_interval.set_value(self.settings.keepalive_interval_seconds)
                    self.keepalive_item.handler_block(self.keepalive_handler)
                    self.keepalive_item.set_active(self.settings.keepalive_enabled)
                    self.keepalive_item.handler_unblock(self.keepalive_handler)
                except (ValueError, OSError):
                    pass
            self.refresh()
            self.render()
            return False

        def cancel_operation(self, *_):
            if self.cancel_event and self.cancel_enabled:
                self.cancel_event.set()
                self.cancel_enabled = False
                self.set_message("Cancelling safely… wait for the operation and any configuration rollback to finish.")
                self.render()

        def disconnect(self, *_):
            if not self.available() or not self.confirm("Disconnect the Sync drive?",
                    "This stops WebDAV and Rclone. Cloud files and saved login are kept. Finish transfers first."):
                return
            from setup_engine import disconnect
            self.start_operation("Disconnecting Sync…", lambda cancel, progress, lazy: disconnect(self.settings, allow_lazy=lazy))

        def update(self, *_):
            if not self.available() or not self.confirm("Update the Sync WebDAV server?",
                    "This temporarily stops Sync and runs the official HTTPS vendor installer as your user. Finish transfers first."):
                return
            from setup_engine import update_server
            self.start_operation("Updating Sync WebDAV…", lambda cancel, progress, lazy:
                                 update_server(self.settings, cancel, progress, lazy), cancellable=True)

        def remove_setup(self, *_):
            if not self.available() or not self.confirm("Remove this app's automatic mount services?",
                    "This stops and removes the two managed user services. Server, saved profile, local credentials, mount folder and cloud files are kept. User lingering is not disabled because other apps may use it."):
                return
            from setup_engine import disconnect
            self.start_operation("Removing automatic mount setup…", lambda cancel, progress, lazy: disconnect(self.settings, True, lazy))

        def enable_logging(self, *_):
            if not self.available() or not self.confirm("Enable private WebDAV failure logging?",
                    "This creates a private daemon log and adds a logging-only service override for the documented setup. "
                    "It does not sign in, stop, restart, or unmount anything. Logging takes effect on the next service start. "
                    "Logs can contain file paths; credentials are masked when displayed. No verbose request logging is enabled."):
                return
            from setup_engine import configure_failure_logging
            self.start_operation("Enabling private failure logging…", lambda cancel, progress, lazy:
                                 configure_failure_logging(self.settings, cancel, progress))

        def open_security_settings(self, *_):
            # Fixed official URL. This only opens the page; no credentials,
            # browser session scraping, policy changes, or automatic recovery.
            try:
                Gio.AppInfo.launch_default_for_uri(SECURITY_URL, None)
            except GLib.Error:
                self.set_message('Could not open the browser. Visit ' + SECURITY_URL)
            return True  # Gtk.LinkButton: prevent a second default URI launch.

        def session_expiry_help(self, *_):
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                       message_type=Gtk.MessageType.INFO,
                                       buttons=Gtk.ButtonsType.CLOSE, text='Check Sync’s account Auto logout policy')
            dialog.format_secondary_text(AUTO_LOGOUT_HINT + '\n\n' + AUTO_LOGOUT_HELP + '\n\n' + KEEPALIVE_POLICY_NOTE)
            dialog.add_button('Open Sync security settings', Gtk.ResponseType.ACCEPT)
            response = dialog.run();dialog.destroy()
            if response == Gtk.ResponseType.ACCEPT:
                self.open_security_settings()

        def about(self, *_):
            dialog = Gtk.AboutDialog(transient_for=self.window, modal=True, program_name=APP_NAME,
                                    version=VERSION, logo_icon_name=APP_ID,
                                    comments="Independent Linux setup and tray application for Sync.com WebDAV.\nNot affiliated with or endorsed by Sync.com.\nGenerated local passwords are stored in private Rclone configuration (obscured, not encrypted).\n\n" + AUTO_LOGOUT_HINT,
                                    website="https://github.com/idrassi/sync-webdav-desktop",
                                    authors=["Mounir IDRASSI"],
                                    copyright="Copyright © 2026 Mounir IDRASSI — MIT License",
                                    license_type=Gtk.License.MIT_X11)
            dialog.run()
            dialog.destroy()

        def toggle_autostart(self, item):
            try:
                set_autostart(item.get_active())
            except (OSError, ValueError) as exc:
                item.handler_block(self.autostart_handler)
                item.set_active(not item.get_active())
                item.handler_unblock(self.autostart_handler)
                self.set_message("Could not change startup setting: " + str(exc))

        def quit_monitor(self, *_):
            if not self.available():
                self.activate()
                return
            self.quit()  # Never stop the Sync services when exiting the monitor.

        def finish_smoke_test(self):
            # Rendering/navigation regression checks only: do not invoke any
            # setup, connection, auth, preferences-write or maintenance action.
            original = self.display_snapshot
            original_busy = self.busy
            presentation_ok = original is not None and getattr(self, 'smoke_first_navigation', False)
            if original:
                for page in PAGES:
                    self.stack.set_visible_child_name(page)
                    presentation_ok = presentation_ok and self.stack.get_visible_child_name() == page
                self.display_snapshot = replace(original, state='auth', configured=True, auth_reason='expired')
                self.render()
                presentation_ok = presentation_ok and self.connection_item.get_label() == 'Sign in & restore…'
                self.busy = 'Testing presentation'
                self.render()
                presentation_ok = presentation_ok and not self.quit_item.get_sensitive() and not self.open_item.get_sensitive()
                self.display_snapshot = original
                self.busy = original_busy
                self.stack.set_visible_child_name('overview')
                self.render()
            entries = [child for child in self.menu.get_children() if not isinstance(child, Gtk.SeparatorMenuItem)]
            pages = [self.stack.child_get_property(child, 'name') for child in self.stack.get_children()]
            passed = (self.snapshot is not None and not self.smoke_errors
                      and self.indicator.get_status() == Indicator.IndicatorStatus.ACTIVE
                      and self.window.get_visible() and len(entries) == len(TRAY_ENTRY_KEYS)
                      and tuple(pages) == PAGES and presentation_ok)
            self.smoke_exit_code = 0 if passed else 1
            print(json.dumps({"smoke_test": "passed" if passed else "failed",
                              "state": self.snapshot.state if self.snapshot else "unknown",
                              "window": self.window.get_title(), "indicator": APP_ID,
                              "errors": self.smoke_errors, 'tray_entries': len(entries), 'pages': pages}), flush=True)
            self.quit()
            return False

        def do_shutdown(self):
            self.closing = True
            if self.cancel_event:
                self.cancel_event.set()
            self.pause_keepalive()
            if hasattr(self, "timer"):
                GLib.source_remove(self.timer)
            if hasattr(self, "indicator"):
                self.indicator.set_status(Indicator.IndicatorStatus.PASSIVE)
            if hasattr(self, 'tray_icons'):
                self.tray_icons.close()
            # Each active operation/read is cancelled above. Python 3.8 does
            # not support shutdown(cancel_futures=...); queued tasks observe
            # their cancellation events before any connection changes.
            self.pool.shutdown(wait=False)
            Gtk.Application.do_shutdown(self)

    application = Application()
    code = application.run(sys.argv)
    return code or application.smoke_exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up, configure and restore your Sync WebDAV Linux drive")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--tray", action="store_true", help="Start without showing the status window")
    parser.add_argument("--status-json", action="store_true", help="Print read-only status without starting the GUI")
    parser.add_argument("--smoke-test", action="store_true", help="Exercise GUI startup, status loading, and shutdown")
    parser.add_argument('--tray-icon-test', action='store_true', help='Test amber/green/red tray rendering without changing services')
    args = parser.parse_args()

    def startup_error(code: str, message: str) -> int:
        if args.status_json:
            print(json.dumps({"error": {"code": code, "message": message}}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 1

    try:
        settings = load_settings()
    except OSError as exc:
        return startup_error("unreadable_settings",
                             f"Could not read settings at {settings_path()}: {redact(str(exc))}. "
                             "Check file and directory permissions.")
    except ValueError as exc:
        return startup_error("invalid_settings",
                             f"Invalid settings at {settings_path()}: {redact(str(exc))}. "
                             "Back up and rename this file, then retry to rediscover the current setup or reconfigure it.")
    if args.status_json:
        try:
            output = json.dumps(collect_snapshot(settings).as_dict(), indent=2)
        except Exception as exc:
            return startup_error("status_unavailable", "Could not collect status: " + redact(str(exc)))
        print(output)
        return 0
    try:
        return run_gui(settings)
    except GuiUnavailable as exc:
        return startup_error("gtk_unavailable", "GTK / Ayatana AppIndicator is required: " + str(exc))


if __name__ == "__main__":
    sys.exit(main())
