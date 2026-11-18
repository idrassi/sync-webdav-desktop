"""Onboarding tests use temporary HOME and mocked services: never touch live Sync."""
import configparser
import contextlib
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import authentication as auth
import backend
import settings as s
import setup_engine as engine


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / '.config')})
        self.env.start()
        self.home_patch = patch.object(s, 'HOME', self.home)
        self.home_patch.start()
        self.config = s.Settings(mount_point=str(self.home / 'Sync Drive'))

    def tearDown(self):
        self.home_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_settings_round_trip_private(self):
        s.save_settings(self.config)
        self.assertEqual(s.load_settings(), self.config)
        self.assertEqual(s.settings_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(s.config_dir().stat().st_mode & 0o777, 0o700)

    def test_injection_and_unsafe_paths_rejected(self):
        for value in [str(self.home), '/', '/tmp/drive', str(self.home / '..' / 'outside'),
                      str(self.home / '.config' / 'drive'), str(self.home / 'drive\nExecStart=evil')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.config, mount_point=value).validate()
        for value in ['bad;profile', '../profile', 'profile\n', 'profile with spaces']:
            with self.assertRaises(ValueError):
                replace(self.config, profile=value).validate()
        for port in [0, 80, 65536, True]:
            with self.assertRaises(ValueError):
                replace(self.config, port=port).validate()
        (self.home / 'alias').symlink_to(self.home)
        with self.assertRaises(ValueError):
            replace(self.config, mount_point=str(self.home / 'alias' / 'drive')).validate()

    def test_managed_destinations_cannot_collide(self):
        for path in [s.settings_path(), s.unit_dir() / 'sync-webdav.service',
                     s.config_dir() / 'credential-recovery/x.json', s.config_dir() / 'backups/x.conf']:
            with self.assertRaises(ValueError):
                replace(self.config, rclone_config=str(path)).validate()

    def test_scope_and_profile_specific_config(self):
        self.assertEqual(replace(self.config, content='vault').scope, 'vault/')
        self.assertEqual(replace(self.config, content='folder', folder='Documents/A').scope, 'Documents/A/')
        self.assertNotEqual(self.config.config_file, replace(self.config, profile='another').config_file)
        self.assertEqual(replace(self.config, content='folder', folder='/Documents/A/').validate().scope,
                         'Documents/A/')
        for folder in ('../bad', '', '/', 'a//b', 'a/./b'):
            with self.subTest(folder=folder), self.assertRaisesRegex(ValueError, 'relative Sync folder'):
                replace(self.config, content='folder', folder=folder).validate()

    def test_installer_pin_validation(self):
        self.assertEqual(replace(self.config, installer_sha256='AB' * 32).validate().installer_sha256, 'ab' * 32)
        for pin in (42, 'not-a-hash', 'z' * 64):
            with self.subTest(pin=pin), self.assertRaisesRegex(ValueError, 'Installer SHA-256'):
                replace(self.config, installer_sha256=pin).validate()

    def test_discover_legacy_does_not_read_secrets(self):
        s.unit_dir().mkdir(parents=True)
        (s.unit_dir() / 'sync-webdav.service').write_text('ExecStart=%h/.sync-webdav/bin/sync-webdav --daemon --port 5010 --profile work --mount vault/\n')
        (s.unit_dir() / 'sync-rclone.service').write_text('ExecStart=/usr/bin/rclone mount work:/ "%h/Sync Drive" --config=%h/.config/rclone/rclone.conf\n')
        found = s.discover_legacy()
        self.assertEqual(found.content, 'vault')
        self.assertEqual(found.port, 5010)
        self.assertEqual(found.profile, 'work')
        self.assertEqual(found.remote, 'work')
        self.assertFalse(found.managed)
        self.assertTrue(found.configured)


class AuthTests(unittest.TestCase):
    def test_chunked_credential_capture(self):
        capture = auth.CredentialCapture()
        capture.feed('Open https://login.example/?token=do-not-log\n  User')
        capture.feed('name: sync\n  Pass')
        capture.feed('word: LOCAL-SECRET\n')
        self.assertEqual(capture.password, 'LOCAL-SECRET')
        capture.clear()
        self.assertIsNone(capture.password)
        self.assertEqual(capture.pending, '')

    def test_does_not_parse_account_password_prompt(self):
        capture = auth.CredentialCapture()
        capture.feed('Password: account-secret\nUsername: not-sync\nPassword: other-secret\n')
        self.assertIsNone(capture.password)

    def test_capture_ignores_unpaired_and_repeated_password_lines(self):
        issued = []
        capture = auth.CredentialCapture(issued.append)
        capture.feed('Username: sync\nPassword: issued\nPassword: unrelated\n')
        capture.feed('Username: sync\nPassword: issued\n')
        self.assertEqual(issued, ['issued'])
        self.assertEqual(capture.password, 'issued')
        capture.feed('Username: sync\nPassword: replacement\n')
        self.assertEqual(issued, ['issued', 'replacement'])

    def test_browser_pipe_handoff_and_no_output_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake-sync'
            binary.write_text('#!/usr/bin/python3\nprint("Open your browser")\nprint("  Username: sync")\nprint("  Password: generated-only")\n')
            binary.chmod(0o700)
            messages = []
            password = auth.browser_login(binary, 'default', threading.Event(), messages.append)
            self.assertEqual(password, 'generated-only')
            self.assertNotIn('generated-only', '\n'.join(messages))

    def test_cancelled_browser_is_terminated(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake-sync'
            binary.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n')
            binary.chmod(0o700)
            cancelled = threading.Event(); cancelled.set()
            with self.assertRaises(auth.Cancelled):
                auth.browser_login(binary, 'default', cancelled, lambda _: None)

    def test_issued_password_persisted_even_if_login_exits_with_error(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake-sync'
            binary.write_text('#!/usr/bin/python3\nimport sys\nprint("Username: sync")\nprint("Password: issued-once")\nsys.exit(1)\n')
            binary.chmod(0o700)
            issued = []
            with self.assertRaises(auth.AuthenticationError):
                auth.browser_login(binary, 'default', threading.Event(), lambda _: None, on_credential=issued.append)
            self.assertEqual(issued, ['issued-once'])

    def test_unknown_login_output_reports_generic_error(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'fake-sync'
            binary.write_text('#!/usr/bin/python3\nimport sys\nprint("token=secret-error")\nsys.exit(1)\n')
            binary.chmod(0o700)
            with self.assertRaises(auth.AuthenticationError) as caught:
                auth.browser_login(binary, 'default', threading.Event(), lambda _: None)
            self.assertNotIn('secret-error', str(caught.exception))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / '.config'),
                                               "XDG_RUNTIME_DIR": str(self.home / 'run')}),
                        patch.object(s, 'HOME', self.home), patch.object(engine, 'HOME', self.home)]
        for p in self.patches: p.start()
        self.config = s.Settings(mount_point=str(self.home / 'Sync Drive'))
        self.cancel = threading.Event()
        self.messages = []

    def tearDown(self):
        for p in self.patches[::-1]: p.stop()
        self.tmp.cleanup()

    def test_rollback_reconciles_new_credential_with_previous_endpoint(self):
        old = replace(self.config, configured=True, managed=True, port=4918)
        s.save_settings(old)
        engine.atomic_write(old.config_file, '[sync-webdav]\nurl=http://127.0.0.1:4918\n')
        def result(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, 'ActiveState=inactive\nUnitFileState=disabled\n', '')
        with patch.object(engine, 'run_command', side_effect=result), patch.object(engine, 'write_rclone') as write, patch('mount_safety.cleanup_stale_mount'):
            transaction = engine.Transaction(replace(old, port=5918))
            transaction.credential_issued('issued-new-password')
            engine.atomic_write(old.config_file, 'new endpoint config')
            transaction.rollback(self.messages.append)
            restored, password, _ = write.call_args.args
            self.assertEqual(restored.port, 4918)
            self.assertEqual(password, 'issued-new-password')
        self.assertIn('4918', old.config_file.read_text())

    def test_generated_units_safe_paths_and_scope(self):
        config = replace(self.config, content='vault', mount_point=str(self.home / 'Drive $cash %unit "test"'))
        files = engine.service_files(config)
        webdav = files[backend.SERVICES[0]]
        rclone = files[backend.SERVICES[1]]
        self.assertIn('"--allow-from" "none"', webdav)
        self.assertIn('"--mount" "vault/"', webdav)
        self.assertIn('$$cash %%unit \\"test\\"', rclone)
        self.assertIn('wait_ready.py', rclone)
        self.assertNotIn('/bin/sh -c', rclone)
        self.assertNotIn('password', webdav.lower())

    def test_installer_pin_checked_before_execution(self):
        script = b'#!/bin/sh\nexit 0\n'
        self.config.config_file.parent.mkdir(parents=True)

        def run(command, *args, **kwargs):
            if command[0] == '/usr/bin/curl':
                self.assertEqual(command[1], '--disable')  # Do not load .curlrc.
                Path(command[command.index('--output') + 1]).write_bytes(script)

        for pin, accepted in [('0' * 64, False), (hashlib.sha256(script).hexdigest(), True), ('', True)]:
            with self.subTest(pin=pin), patch.object(engine, 'run', side_effect=run) as mock_run:
                config = replace(self.config, installer_sha256=pin)
                if accepted:
                    engine.install_server(config, self.cancel, self.messages.append, update=True)
                    self.assertEqual(mock_run.call_args_list[1].args[0][0], '/bin/sh')
                    self.assertEqual(mock_run.call_args_list[2].args[0], [str(engine.WEBDAV_BIN), '--version'])
                else:
                    with self.assertRaisesRegex(backend.OperationError, 'SHA-256 pin'):
                        engine.install_server(config, self.cancel, self.messages.append, update=True)
                    mock_run.assert_called_once()

    def test_distro_commands_fixed_and_documented(self):
        self.assertEqual(engine.package_commands('debian')[0], ['/usr/bin/apt-get', 'update'])
        self.assertIn('-Syu', engine.package_commands('arch')[0])
        self.assertIn('rclone', engine.package_commands('fedora')[0])
        with self.assertRaises(backend.OperationError): engine.package_commands('unknown')

    def test_obscure_stdin_and_preserve_other_remotes(self):
        self.config.config_file.parent.mkdir(parents=True)
        self.config.config_file.write_text('[other]\ntype = local\n')
        with patch.object(engine, 'run', return_value='OBSCURED_123\n') as run:
            engine.write_rclone(self.config, 'LOCAL-SECRET', self.cancel)
        args = run.call_args.args[0]
        self.assertEqual(args[-2:], ['obscure', '-'])
        self.assertNotIn('LOCAL-SECRET', ' '.join(args))
        self.assertEqual(run.call_args.kwargs['input_text'], 'LOCAL-SECRET\n')
        data = self.config.config_file.read_text()
        self.assertNotIn('LOCAL-SECRET', data)
        self.assertIn('[other]', data)
        self.assertIn('[sync-webdav]', data)
        self.assertEqual(self.config.config_file.stat().st_mode & 0o777, 0o600)

    def test_rclone_failure_never_includes_secret_output(self):
        with self.assertRaises(backend.OperationError) as caught:
            engine.run(['/usr/bin/python3', '-c', 'import sys; print("pass=secret"); sys.exit(1)'], self.cancel, secret=True)
        self.assertNotIn('secret', str(caught.exception))

    def test_cancellable_commands_and_timeout(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(auth.Cancelled):
            engine.run(['/usr/bin/true'], cancel)
        with self.assertRaises(backend.OperationError):
            engine.run(['/usr/bin/python3', '-c', 'import time; time.sleep(10)'], self.cancel, timeout=1)

    def test_transaction_backup_and_rollback_local_files(self):
        s.unit_dir().mkdir(parents=True)
        unit = s.unit_dir() / backend.SERVICES[0]
        unit.write_text('original service')
        def result(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, 'ActiveState=inactive\nUnitFileState=disabled\n', '')
        with patch.object(engine, 'run_command', side_effect=result), patch('mount_safety.cleanup_stale_mount'):
            transaction = engine.Transaction(self.config)
            unit.write_text('changed')
            engine.atomic_write(self.config.config_file, 'new config')
            transaction.rollback(self.messages.append)
        self.assertEqual(unit.read_text(), 'original service')
        self.assertFalse(self.config.config_file.exists())
        self.assertEqual(transaction.directory.stat().st_mode & 0o777, 0o700)
        for file in transaction.directory.iterdir(): self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_failed_setup_preserves_concurrent_desktop_changes(self):
        report = engine.Preflight('debian', (), (), (), True, False)
        paths = [self.home / '.config/gtk-3.0/bookmarks', backend.autostart_path()]
        for original in (None, 'old entry\n'):
            with self.subTest(original=original):
                def login(*args, **kwargs):
                    for path in paths:
                        engine.atomic_write(path, 'new user entry\n')
                    raise auth.AuthenticationError('Sign-in failed')

                for path in paths:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        engine.atomic_write(path, original)
                with patch.object(engine, 'preflight', return_value=report), \
                     patch.object(engine, 'operation_lock', contextlib.nullcontext), \
                     patch.object(engine, 'install_requirements'), patch.object(engine, 'install_server'), \
                     patch.object(engine, 'ensure_port_free'), patch.object(engine, 'browser_login', side_effect=login), \
                     patch.object(engine, 'run_command', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
                     patch('mount_safety.cleanup_stale_mount'):
                    with self.assertRaisesRegex(auth.AuthenticationError, 'Sign-in failed'):
                        engine.connect(self.config, self.cancel, self.messages.append)
                for path in paths:
                    self.assertEqual(path.read_text(), 'new user entry\n')
                executables = {call.args[0][0] for call in run.call_args_list}
                self.assertEqual(executables, {'/usr/bin/loginctl', '/usr/bin/systemctl'})

    def test_setup_success_only_browser_auth_and_service_calls(self):
        report = engine.Preflight('debian', (), (), (), True, False)
        snapshot = type('Snapshot', (), {'state': 'online'})()
        with patch.object(engine, 'preflight', return_value=report), \
             patch.object(engine, 'operation_lock', contextlib.nullcontext), \
             patch.object(engine, 'install_requirements'), patch.object(engine, 'install_server'), \
             patch.object(engine, 'Transaction') as tx, patch.object(engine, 'ensure_port_free'), \
             patch.object(engine, 'browser_login', return_value='local-only') as login, \
             patch.object(engine, 'write_rclone') as config, patch.object(engine, 'run'), \
             patch.object(engine, 'run_command', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             patch.object(engine, 'wait_server'), patch.object(engine, 'rclone_test'), \
             patch.object(engine, 'collect_snapshot', return_value=snapshot), \
             patch.object(engine, 'set_autostart', side_effect=PermissionError) as autostart, \
             patch.object(engine, 'add_bookmark', side_effect=OSError) as bookmark, \
             patch('mount_safety.cleanup_stale_mount'):
            result = engine.connect(self.config, self.cancel, self.messages.append)
        self.assertTrue(result.managed)
        self.assertTrue(result.configured)
        self.assertEqual(config.call_count, 1)
        self.assertEqual(config.call_args.args[:2], (self.config, 'local-only'))
        self.assertFalse(config.call_args.args[2].is_set())
        self.assertNotIn('local-only', '\n'.join(self.messages))
        tx.return_value.rollback.assert_not_called()
        autostart.assert_called_once_with(True)
        bookmark.assert_called_once_with(self.config)
        self.assertEqual(s.load_settings(), result)
        self.assertIn('Could not update tray startup', self.messages[-1])
        self.assertIn('Could not add the Files bookmark', self.messages[-1])

    def test_setup_failure_rolls_back_and_requires_reset_approval(self):
        report = engine.Preflight('debian', (), (), (), True, False)
        with patch.object(engine, 'preflight', return_value=report), \
             patch.object(engine, 'operation_lock', contextlib.nullcontext), \
             patch.object(engine, 'install_requirements'), patch.object(engine, 'install_server'), \
             patch.object(engine, 'Transaction') as tx, patch.object(engine, 'ensure_port_free'), \
             patch.object(engine, 'browser_login', return_value=None), patch.object(engine, 'run_command', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             patch('mount_safety.cleanup_stale_mount'):
            with self.assertRaisesRegex(backend.OperationError, 'already has a local WebDAV password'):
                engine.connect(self.config, self.cancel, self.messages.append)
            tx.return_value.rollback.assert_called_once()

    def test_adoption_requires_approval(self):
        report = engine.Preflight('debian', (), (), (), True, True)
        with patch.object(engine, 'preflight', return_value=report):
            with self.assertRaisesRegex(backend.OperationError, 'adoption approval'):
                engine.connect(self.config, self.cancel, self.messages.append)

    def test_recovery_record_survives_config_rollback(self):
        def result(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, 'ActiveState=inactive\nUnitFileState=disabled\n', '')
        with patch.object(engine, 'run_command', side_effect=result), patch('mount_safety.cleanup_stale_mount'):
            transaction = engine.Transaction(self.config)
            record = engine.preserve_credential(self.config, 'issued-local-password')
            engine.atomic_write(self.config.config_file, 'new config')
            transaction.rollback(self.messages.append)
        self.assertFalse(self.config.config_file.exists())
        self.assertTrue(record.exists())
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(record.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(engine.recovered_credential(self.config)[0], 'issued-local-password')
        engine.clear_recovery(self.config)
        self.assertFalse(record.exists())

    def test_recovery_keeps_latest_per_profile_and_migrates_legacy_files(self):
        directory = s.config_dir() / 'credential-recovery'
        other = replace(self.config, profile=self.config.profile + '-other')
        other_record = engine.preserve_credential(other, 'other-secret')
        old = directory / f'{self.config.profile}-{uuid.uuid4()}.json'
        engine.atomic_write(old, json.dumps({'profile': self.config.profile, 'username': 'sync', 'password': 'old-secret'}))
        self.assertEqual(engine.recovered_credential(self.config), ('old-secret', old))
        new = engine.preserve_credential(self.config, 'new-secret')
        self.assertFalse(old.exists())
        self.assertEqual(engine.preserve_credential(self.config, 'newest-secret'), new)
        self.assertEqual(engine.recovered_credential(self.config), ('newest-secret', new))
        self.assertEqual(set(directory.iterdir()), {new, other_record})
        with patch.object(engine, 'atomic_write', side_effect=OSError('disk full')), self.assertRaises(OSError):
            engine.preserve_credential(self.config, 'write-failed')
        self.assertEqual(engine.recovered_credential(self.config), ('newest-secret', new))
        engine.clear_recovery(self.config)
        self.assertEqual(list(directory.iterdir()), [other_record])

    def test_bookmark_keeps_other_entries_and_no_duplicates(self):
        path = self.home / '.config/gtk-3.0/bookmarks'
        path.parent.mkdir(parents=True)
        path.write_text('file:///other Other\n')
        engine.add_bookmark(self.config)
        engine.add_bookmark(self.config)
        self.assertEqual(path.read_text().count('Sync Drive'), 1)
        self.assertIn('file:///other Other', path.read_text())

    def test_never_remove_unmanaged_services(self):
        with patch.object(engine, 'operation_lock', contextlib.nullcontext), patch.object(engine, 'change_services') as change:
            with self.assertRaisesRegex(backend.OperationError, 'managed'):
                engine.disconnect(self.config, remove_services=True)
            change.assert_not_called()


if __name__ == '__main__':
    unittest.main()
