"""Compact menu policy tests require no GTK or connection actions."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from backend import Snapshot, Unit
from ui_policy import (PAGES, TRAY_ENTRY_KEYS, WebDAVTimeoutGrace, connection_notice,
                       tray_state)


class MenuPolicyTests(unittest.TestCase):
    def snapshot(self, state='online', configured=True):
        return SimpleNamespace(state=state, configured=configured)

    def test_compact_stable_menu_and_pages(self):
        self.assertEqual(len(TRAY_ENTRY_KEYS), 6)
        self.assertEqual(PAGES, ('overview', 'preferences', 'diagnostics', 'help'))
        self.assertNotIn('update', TRAY_ENTRY_KEYS)
        self.assertNotIn('remove', TRAY_ENTRY_KEYS)
        self.assertNotIn('keepalive', TRAY_ENTRY_KEYS)

    def test_connected_action_is_disconnect(self):
        policy = tray_state(self.snapshot())
        self.assertEqual(policy.summary, 'Connected')
        self.assertEqual(policy.action, 'disconnect')
        self.assertTrue(policy.open_enabled)
        self.assertTrue(policy.quit_enabled)

    def test_expired_action_keeps_recovery_prominent(self):
        policy = tray_state(self.snapshot('auth'))
        self.assertEqual(policy.action, 'restore')
        self.assertEqual(policy.action_label, 'Sign in & restore…')
        self.assertFalse(policy.open_enabled)

    def test_new_user_connect_opens_setup(self):
        policy = tray_state(self.snapshot('setup', configured=False))
        self.assertEqual(policy.action, 'setup')
        self.assertEqual(policy.summary, 'Not configured')

    def test_disconnected_existing_setup_connects_without_new_setup(self):
        policy = tray_state(self.snapshot('offline'))
        self.assertEqual(policy.action, 'restart')
        self.assertEqual(policy.action_label, 'Connect')

    def test_busy_cancel_only_when_supported(self):
        policy = tray_state(self.snapshot(), busy=True, can_cancel=True)
        self.assertEqual(policy.action, 'cancel')
        self.assertTrue(policy.action_enabled)
        self.assertFalse(policy.quit_enabled)
        self.assertFalse(policy.open_enabled)
        policy = tray_state(self.snapshot(), busy=True, can_cancel=False)
        self.assertEqual(policy.action, 'none')
        self.assertFalse(policy.action_enabled)

    def test_unknown_or_connecting_never_triggers_duplicate_connection(self):
        self.assertFalse(tray_state(None).action_enabled)
        self.assertFalse(tray_state(self.snapshot('working')).action_enabled)


class WebDAVTimeoutGraceTests(unittest.TestCase):
    def setUp(self):
        self.grace = WebDAVTimeoutGrace()
        self.online = Snapshot(
            webdav=Unit('sync-webdav.service', 'active', invocation='webdav'),
            rclone=Unit('sync-rclone.service', 'active', invocation='rclone'),
            mounted=True, mount_type='fuse.rclone sync-webdav:', http_status=401,
            auth_failure=False, state='online', title='Connected', detail='', logs='',
            checked_at='13:49:00', configured=True)
        self.timeout = replace(self.online, http_status=None, http_timed_out=True, state='offline')

    def test_intermittent_timeouts_do_not_change_tray_state_or_notify(self):
        previous_state = None
        for reading in (self.online, self.timeout, self.timeout, self.online,
                        self.timeout, self.online, self.timeout, self.timeout):
            shown = self.grace.update(reading)
            self.assertEqual(tray_state(shown).summary, 'Connected')
            self.assertIsNone(connection_notice(previous_state, shown))
            self.assertEqual(shown.http_timed_out, reading.http_timed_out)
            if reading.http_timed_out:
                self.assertIn('checking WebDAV', shown.title)
            previous_state = shown.state

    def test_sustained_timeout_warns_once_and_recovers_on_success(self):
        self.grace.update(self.online)
        self.assertEqual(self.grace.update(self.timeout).state, 'online')
        self.assertEqual(self.grace.update(self.timeout).state, 'online')
        shown = self.grace.update(self.timeout)
        self.assertEqual(shown.state, 'offline')
        self.assertEqual(connection_notice('online', shown)[0], 'Sync WebDAV is not responding')
        self.assertIsNone(connection_notice('offline', self.grace.update(self.timeout)))
        self.assertIs(self.grace.update(self.online), self.online)
        self.assertEqual(self.grace.update(self.timeout).state, 'online')

    def test_hard_failures_bypass_grace_and_alert_immediately(self):
        for failure in (replace(self.timeout, mounted=False),
                        replace(self.timeout, webdav=replace(self.online.webdav, active='failed')),
                        replace(self.timeout, rclone=replace(self.online.rclone, active='inactive')),
                        replace(self.timeout, rclone=replace(self.online.rclone, active='activating'), state='working'),
                        replace(self.timeout, http_timed_out=False),
                        replace(self.timeout, http_timed_out=False, http_status=500),
                        replace(self.timeout, auth_failure=True, state='auth', title='Sign in')):
            with self.subTest(failure=failure):
                self.grace.update(self.online)
                pending = self.grace.update(self.timeout)
                shown = self.grace.update(failure)
                self.assertIs(shown, failure)
                self.assertIsNotNone(connection_notice(pending.state, shown))
                self.assertEqual(self.grace.update(self.timeout).state, 'offline')

    def test_grace_requires_previously_healthy_unchanged_connection(self):
        self.assertEqual(self.grace.update(self.timeout).state, 'offline')
        self.grace.update(self.online)
        restarted = replace(self.timeout, webdav=replace(self.online.webdav, invocation='new'))
        self.assertEqual(self.grace.update(restarted).state, 'offline')
        self.grace.update(self.online)
        self.assertEqual(self.grace.update(replace(self.timeout, port=4930)).state, 'offline')

    def test_recovery_resets_grace(self):
        for reading, busy in ((self.timeout, True), (replace(self.timeout, recovery_busy=True), False)):
            self.grace.update(self.online)
            self.assertIs(self.grace.update(reading, busy), reading)
            self.assertEqual(self.grace.update(self.timeout).state, 'offline')


if __name__ == '__main__':
    unittest.main()
