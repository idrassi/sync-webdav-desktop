"""Regression coverage for recovery order; all service/unmount/auth calls mocked."""
import contextlib
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import diagnostics
import setup_engine as engine
import settings
from backend import SERVICES, SYSTEMCTL, OperationError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.home / '.config'),
                                          'XDG_RUNTIME_DIR': str(self.home / 'run')})
        self.env.start()
        self.config = settings.Settings()
        self.cancel = threading.Event()
        self.messages = []

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def test_busy_cleanup_blocks_browser_auth_and_service_start(self):
        from mount_safety import StaleMountBusy
        with patch.object(engine, 'operation_lock', contextlib.nullcontext), \
             patch.object(engine, 'change_services') as stop, \
             patch('mount_safety.cleanup_stale_mount', side_effect=StaleMountBusy('busy')), \
             patch.object(engine, 'browser_login') as login, patch.object(engine, 'run') as run:
            with self.assertRaises(StaleMountBusy):
                engine.restore(self.config, self.cancel, self.messages.append)
        stop.assert_called_once_with('stop')
        login.assert_not_called()
        run.assert_not_called()

    def test_restore_cleanup_and_logging_before_browser(self):
        calls = []
        def service(args, *_, **kwargs):
            calls.append('start:' + args[-1])
            return ''
        with patch.object(engine, 'operation_lock', contextlib.nullcontext), \
             patch.object(engine, 'change_services', side_effect=lambda action: calls.append(action)), \
             patch('mount_safety.cleanup_stale_mount', side_effect=lambda *a, **kw: calls.append('cleanup:' + str(kw['allow_lazy']))), \
             patch.object(engine, 'enable_failure_logging', side_effect=lambda *a: calls.append('logging')), \
             patch.object(engine, 'browser_login', side_effect=lambda *a, **kw: calls.append('browser')), \
             patch.object(engine, 'recovered_credential', return_value=(None, None)), \
             patch.object(engine, 'run', side_effect=service), patch.object(engine, 'wait_server'), \
             patch.object(engine, 'rclone_test'), patch.object(engine, 'clear_recovery'), \
             patch.object(engine, 'collect_snapshot', return_value=type('Snapshot', (), {'state': 'online'})()):
            engine.restore(self.config, self.cancel, self.messages.append, allow_lazy=True)
        self.assertEqual(calls, ['stop', 'cleanup:True', 'logging', 'browser',
                                 'start:' + SERVICES[0], 'start:' + SERVICES[1]])

    def test_restart_never_signs_in(self):
        with patch.object(engine, 'operation_lock', contextlib.nullcontext), \
             patch.object(engine, 'stop_and_cleanup') as cleanup, \
             patch.object(engine, 'enable_failure_logging'), patch.object(engine, 'run'), \
             patch.object(engine, 'wait_server'), patch.object(engine, 'rclone_test'), \
             patch.object(engine, 'collect_snapshot', return_value=type('Snapshot', (), {'state': 'online'})()), \
             patch.object(engine, 'browser_login') as login:
            engine.restart(self.config, self.cancel, self.messages.append)
        cleanup.assert_called_once_with(self.config, self.cancel, self.messages.append, False)
        login.assert_not_called()

    def test_logging_configuration_does_not_start_or_stop_services(self):
        settings.unit_dir().mkdir(parents=True)
        unit = settings.unit_dir() / SERVICES[0]
        unit.write_text('ExecStart=%h/.sync-webdav/bin/sync-webdav --daemon --allow-from none\n')
        with patch.object(engine, 'operation_lock', contextlib.nullcontext), \
             patch.object(engine, 'run', return_value='') as run, \
             patch.object(engine, 'change_services') as service:
            engine.configure_failure_logging(self.config, self.cancel, self.messages.append)
        self.assertEqual(run.call_args.args[0], [SYSTEMCTL, '--user', 'daemon-reload'])
        service.assert_not_called()
        self.assertTrue(diagnostics.logging_enabled())
        self.assertEqual(diagnostics.log_path().stat().st_mode & 0o777, 0o600)
        self.assertNotIn('--log-file', unit.read_text())

    def test_failed_logging_reload_rolls_back_override(self):
        settings.unit_dir().mkdir(parents=True)
        unit = settings.unit_dir() / SERVICES[0]
        unit.write_text('ExecStart=%h/.sync-webdav/bin/sync-webdav --daemon --allow-from none\n')
        with patch.object(engine, 'run', side_effect=OperationError('reload failed')), \
             patch.object(engine, 'run_command', return_value=subprocess.CompletedProcess([], 0, '', '')):
            with self.assertRaises(OperationError):
                engine.enable_failure_logging(self.config, self.cancel, self.messages.append)
        override = settings.unit_dir() / (SERVICES[0] + '.d') / diagnostics.LOG_DROPIN
        self.assertFalse(override.exists())


if __name__ == '__main__':
    unittest.main()
