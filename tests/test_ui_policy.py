"""Compact menu policy tests require no GTK or connection actions."""
from types import SimpleNamespace
import unittest
from ui_policy import tray_state, TRAY_ENTRY_KEYS, PAGES


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


if __name__ == '__main__':
    unittest.main()
