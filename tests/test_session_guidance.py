"""Policy guidance tests are pure/read-only; no browser or account operations."""
import unittest
from backend import Unit, classify
from session_guidance import AUTO_LOGOUT_HINT, AUTO_LOGOUT_HELP, KEEPALIVE_POLICY_NOTE, SECURITY_URL


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.server = Unit('sync-webdav.service', 'inactive', 'dead')
        self.clone = Unit('sync-rclone.service', 'inactive', 'dead')

    def test_expiry_explains_account_policy_without_claiming_selected_value(self):
        state, title, detail = classify(self.server, self.clone, False, None, True, auth_reason='expired')
        self.assertEqual(state, 'auth')
        self.assertIn(AUTO_LOGOUT_HINT, detail)
        self.assertIn('Auto logout', detail)
        self.assertIn('does not read or change', detail)
        self.assertIn('Sign in & restore', detail)
        self.assertNotIn('your policy is 1 hour', detail.lower())

    def test_other_auth_failures_are_not_misdiagnosed_as_auto_logout(self):
        for reason in ['unknown', 'revoked', 'missing', 'invalid']:
            _, _, detail = classify(self.server, self.clone, False, None, True, auth_reason=reason)
            self.assertNotIn(AUTO_LOGOUT_HINT, detail)

    def test_healthy_state_does_not_show_expiry_warning(self):
        server = Unit('sync-webdav.service', 'active', 'running')
        clone = Unit('sync-rclone.service', 'active', 'running')
        _, _, detail = classify(server, clone, True, 401, False)
        self.assertNotIn('Auto logout', detail)

    def test_guidance_preserves_security_choice_and_does_not_promise_keepalive_fix(self):
        self.assertEqual(SECURITY_URL, 'https://cp.sync.com/account/security')
        for value in ['Never', '1 hour', '24 hours']:
            self.assertIn(value, AUTO_LOGOUT_HELP)
        self.assertIn('Other causes', AUTO_LOGOUT_HELP)
        self.assertIn('shared', AUTO_LOGOUT_HELP)
        self.assertIn('cannot override', KEEPALIVE_POLICY_NOTE)
        self.assertIn('may disable', KEEPALIVE_POLICY_NOTE)


if __name__ == '__main__':
    unittest.main()
