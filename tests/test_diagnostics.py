import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import backend
import diagnostics as d
import settings


class DiagnosticTests(unittest.TestCase):
    def event(self, message, unit='sync-webdav.service', invocation='a'):
        return {'MESSAGE': message, '_SYSTEMD_USER_UNIT': unit, '_SYSTEMD_INVOCATION_ID': invocation}

    def test_unknown_sdk_failure_is_not_proof_of_revocation(self):
        events = [self.event('SDK session check failed (0)'), self.event('Auth rejected: revoked.')]
        self.assertEqual(d.auth_observation(events), 'unknown')
        self.assertEqual(d.auth_observation([self.event('Auth rejected: revoked.')]), 'revoked')

    def test_original_expiry_survives_later_unknown_retries(self):
        events = [self.event('Auth rejected: expired.'), self.event('SDK session check failed (0)', invocation='b'),
                  self.event('Auth rejected: revoked.', invocation='b')]
        self.assertEqual(d.original_auth_cause(events), 'expired')
        self.assertEqual(d.auth_observation(events), 'unknown')

    def test_banner_does_not_clear_previous_auth_reason(self):
        events = [self.event('Auth rejected: expired.'), self.event('Sync WebDAV Server v1.2.1', invocation='b')]
        self.assertEqual(d.auth_observation(events), 'expired')
        events.append(self.event('Started sync-webdav.service - Server', unit=''))
        self.assertEqual(d.auth_observation(events), '')

    def test_unrelated_failures_and_rclone_ignored(self):
        events = [self.event('Auth rejected: expired.'), self.event('Address already in use')]
        self.assertEqual(d.auth_observation(events), '')
        self.assertEqual(d.auth_observation([self.event('Auth rejected: expired.', unit='sync-rclone.service')]), '')

    def test_specific_status_and_stale_warning(self):
        server = backend.Unit('sync-webdav.service', 'activating', 'auto-restart')
        clone = backend.Unit('sync-rclone.service', 'failed', 'failed')
        state, title, detail = backend.classify(server, clone, True, None, True,
                                              auth_reason='expired', stale_mount=True)
        self.assertEqual(state, 'auth')
        self.assertIn('expired', title)
        self.assertIn('stale', detail)
        _, title, detail = backend.classify(server, clone, True, None, True, auth_reason='unknown')
        self.assertIn('validation failed', title)
        self.assertIn('not proof', detail)
        _, title, _ = backend.classify(server, clone, True, None, False, stale_mount=True)
        self.assertIn('stale mount', title)

    def test_private_log_and_masked_bounded_tail(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'XDG_CONFIG_HOME': temporary}):
            path = d.prepare_log()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            path.write_text('Auth rejected: expired.\nPassword: secret-value\ntoken=private-token\n')
            output = d.daemon_log_tail()
            self.assertIn('expired', output)
            self.assertNotIn('secret-value', output)
            self.assertNotIn('private-token', output)
            path.write_text('x' * 100000 + '\nlast-line\n')
            self.assertLess(len(d.daemon_log_tail()), d.MAX_TAIL + 10)
            path.chmod(0o644)
            with self.assertRaises(ValueError): d.daemon_log_tail()

    def test_log_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'XDG_CONFIG_HOME': temporary}):
            target = Path(temporary) / 'target'
            target.write_text('leave me')
            path = d.log_path()
            path.parent.mkdir(parents=True)
            path.symlink_to(target)
            with self.assertRaises(ValueError): d.prepare_log()
            self.assertEqual(target.read_text(), 'leave me')

    def test_logging_override_preserves_documented_command(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'XDG_CONFIG_HOME': temporary}):
            settings.unit_dir().mkdir(parents=True)
            unit = settings.unit_dir() / 'sync-webdav.service'
            binary = Path.home() / '.sync-webdav/bin/sync-webdav'
            unit.write_text('ExecStart=%h/.sync-webdav/bin/sync-webdav --daemon --allow-from none\n')
            text = d.legacy_logging_dropin(binary)
            self.assertIn('ExecStart=\nExecStart=', text)
            self.assertIn('"--log-file"', text)
            self.assertIn('UMask=0077', text)
            unit.write_text('ExecStart=/bin/sh -c "custom command"\n')
            with self.assertRaises(ValueError): d.legacy_logging_dropin(binary)

    def test_logging_is_detected_in_escaped_config_path(self):
        with tempfile.TemporaryDirectory(prefix='config $cash %unit ') as temporary, patch.dict(os.environ, {'XDG_CONFIG_HOME': temporary}):
            settings.unit_dir().mkdir(parents=True)
            unit = settings.unit_dir() / 'sync-webdav.service'
            binary = Path.home() / '.sync-webdav/bin/sync-webdav'
            unit.write_text('ExecStart=%h/.sync-webdav/bin/sync-webdav --daemon --allow-from none\n')
            override = settings.unit_dir() / 'sync-webdav.service.d' / d.LOG_DROPIN
            override.parent.mkdir()
            override.write_text(d.legacy_logging_dropin(binary))
            self.assertTrue(d.logging_enabled())

    def test_commented_or_overridden_logging_is_not_enabled(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'XDG_CONFIG_HOME': temporary}):
            settings.unit_dir().mkdir(parents=True)
            unit = settings.unit_dir() / 'sync-webdav.service'
            unit.write_text('[Service]\n# ExecStart=--log-file ' + str(d.log_path()) + '\n')
            self.assertFalse(d.logging_enabled())
            binary = Path.home() / '.sync-webdav/bin/sync-webdav'
            unit.write_text('ExecStart=%h/.sync-webdav/bin/sync-webdav --daemon --allow-from none\n')
            override = settings.unit_dir() / 'sync-webdav.service.d' / d.LOG_DROPIN
            override.parent.mkdir()
            override.write_text(d.legacy_logging_dropin(binary))
            self.assertTrue(d.logging_enabled())
            (override.parent / '99-custom.conf').write_text('[Service]\nExecStart=\nExecStart=/custom/program\n')
            self.assertFalse(d.logging_enabled())

    def test_original_daemon_failure_uses_start_marker_not_latest_retry(self):
        text = ('[sync-desktop-start 1999999980.000000]\nAuth rejected: expired.\n'
                '[sync-desktop-start 1999999995.000000]\nSDK session check failed (0)\nAuth rejected: revoked.\n')
        with patch('time.time', return_value=2000000000), patch('time.monotonic', return_value=10000):
            self.assertEqual(d.daemon_auth_cause(text, 9990 * 1000000), 'expired')
            self.assertEqual(d.daemon_auth_cause(text, 9997 * 1000000), 'unknown')
            self.assertEqual(d.daemon_auth_cause('Auth rejected: expired.', 9990 * 1000000), '')

    def test_failure_window_is_short_and_boot_relative(self):
        with patch('time.time', return_value=2000000000), patch('time.monotonic', return_value=10000):
            start, end = d.failure_window(9990 * 1000000)
            self.assertLess(start, end)
            self.assertIsNone(d.failure_window(0))


if __name__ == '__main__':
    unittest.main()
