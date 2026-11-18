"""Exercise CLI and monitor callbacks without GTK, live services or a display."""
from concurrent.futures import Future
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
from settings import Settings


class StartupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name) / 'sync-webdav-desktop'
        self.directory.mkdir()
        self.path = self.directory / 'settings.json'
        env = patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['app.py', *args]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = app.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_invalid_settings_do_not_start_gtk(self):
        self.path.write_text('{"mount_point": "/"}')
        with patch.object(app, 'run_gui') as gui:
            code, stdout, stderr = self.run_main()
        gui.assert_not_called()
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('Invalid settings', stderr)
        self.assertIn(str(self.path), stderr)
        self.assertIn('Back up and rename', stderr)
        self.assertNotIn('GTK', stderr)

    def test_invalid_status_settings_return_json_without_dumping_content(self):
        for raw in ('{"token": "dont-print-me"', '{"content": ["files"]}', '[]'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                code, stdout, stderr = self.run_main('--status-json')
                self.assertEqual((code, stderr), (1, ''))
                self.assertEqual(json.loads(stdout)['error']['code'], 'invalid_settings')
                self.assertNotIn('dont-print-me', stdout)

    def test_settings_permission_error_for_both_entry_points(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError(errno.EACCES, 'Permission denied', self.path)):
            code, stdout, stderr = self.run_main()
            self.assertEqual((code, stdout), (1, ''))
            self.assertIn('Check file and directory permissions', stderr)
            code, stdout, stderr = self.run_main('--status-json')
            self.assertEqual((code, stderr), (1, ''))
            self.assertEqual(json.loads(stdout)['error']['code'], 'unreadable_settings')

    def test_status_failure_is_structured_and_redacted(self):
        with patch.object(app, 'collect_snapshot', side_effect=RuntimeError('password=local-secret')):
            code, stdout, stderr = self.run_main('--status-json')
        self.assertEqual((code, stderr), (1, ''))
        self.assertEqual(json.loads(stdout)['error']['code'], 'status_unavailable')
        self.assertNotIn('local-secret', stdout)

    def test_success_preserves_status_json_format(self):
        snapshot = SimpleNamespace(as_dict=lambda: {'state': 'online'})
        with patch.object(app, 'collect_snapshot', return_value=snapshot):
            code, stdout, stderr = self.run_main('--status-json')
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(json.loads(stdout), {'state': 'online'})

    def test_gtk_message_is_reserved_for_gtk_initialization(self):
        with patch.dict(sys.modules, {'gi': None}):
            code, stdout, stderr = self.run_main()
        self.assertEqual((code, stdout), (1, ''))
        self.assertIn('GTK / Ayatana AppIndicator is required', stderr)


class MonitorTests(unittest.TestCase):
    def exercise(self, test):
        class FakeApplication:
            def __init__(self, **kwargs):
                pass

            def run(application, argv):
                try:
                    test(application)
                finally:
                    application.pool.shutdown(wait=False)
                return 0

        gi = ModuleType('gi')
        gi.require_version = Mock()
        repository = ModuleType('gi.repository')
        repository.Gtk = SimpleNamespace(Application=FakeApplication)
        repository.Gio = SimpleNamespace(ApplicationFlags=SimpleNamespace(HANDLES_COMMAND_LINE=0))
        repository.GLib = SimpleNamespace(idle_add=Mock())
        repository.AyatanaAppIndicator3 = None
        with patch.dict(sys.modules, {'gi': gi, 'gi.repository': repository}):
            self.assertEqual(app.run_gui(Settings()), 0)

    def test_tick_survives_refresh_and_keepalive_exceptions(self):
        def check(gui):
            gui.keepalive_tick = Mock(side_effect=[RuntimeError('render failed'), None])
            with patch.object(gui.pool, 'submit', side_effect=[RuntimeError('token=private'), Future()]) as submit:
                with self.assertLogs(app.LOG, 'WARNING') as log:
                    self.assertTrue(gui.tick())
                    self.assertFalse(gui.refreshing)
                    self.assertTrue(gui.tick())
                self.assertTrue(gui.refreshing)
                self.assertNotIn('private', '\n'.join(log.output))
                self.assertEqual(submit.call_count, 2)
                self.assertTrue(gui.tick())
                self.assertEqual(gui.keepalive_tick.call_count, 2)
            gui.closing = True
            self.assertFalse(gui.tick())
        self.exercise(check)

    def test_keepalive_completion_refreshes_even_if_render_fails(self):
        def check(gui):
            gui.keepalive_future = future = Future()
            future.set_result(app.KeepAliveResult('success', 'Read complete', 'now'))
            gui.refresh = Mock()
            gui.render = Mock(side_effect=ValueError('render error'))
            with self.assertLogs(app.LOG, 'WARNING'):
                self.assertFalse(gui.keepalive_finished(0, future))
            self.assertIsNone(gui.keepalive_future)
            gui.refresh.assert_called_once_with()
        self.exercise(check)

    def test_keepalive_submit_failure_releases_schedule_claim(self):
        def check(gui):
            gui.snapshot = SimpleNamespace(state='online', stale_mount=False, recovery_busy=False)
            schedule = gui.keepalive_schedule
            schedule.online = True
            schedule.next_due = 0
            with patch.object(gui.pool, 'submit', side_effect=RuntimeError('executor unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'executor unavailable'):
                    gui.keepalive_tick()
            self.assertIsNone(schedule.pending)
            self.assertIsNone(gui.keepalive_future)
            self.assertIsNone(gui.keepalive_cancel)
        self.exercise(check)

    def test_expiry_failure_retries_after_backoff_and_warns_once(self):
        def check(gui):
            snapshot = SimpleNamespace(auth_reason='expired', session_issued_at='issued',
                                       webdav=SimpleNamespace(active_exit_monotonic=123))
            future = Future()
            future.set_result(snapshot)
            failed = Future()
            failed.set_exception(OSError('history unavailable'))
            gui.timeout_grace = Mock()
            gui.render = Mock()
            gui.set_message = Mock()
            with patch.object(gui.pool, 'submit', return_value=failed) as submit, patch.object(app.time, 'monotonic') as clock:
                clock.return_value = 100
                gui.refreshed(future)
                gui.expiry_recorded(('issued', 123), failed)
                clock.return_value += app.EXPIRY_RETRY_SECONDS - 1
                gui.refreshed(future)
                self.assertEqual(submit.call_count, 1)
                clock.return_value += 1
                gui.refreshed(future)
                self.assertEqual(submit.call_count, 2)
                gui.expiry_recorded(('issued', 123), failed)
                gui.set_message.assert_called_once()
        self.exercise(check)


if __name__ == '__main__':
    unittest.main()
