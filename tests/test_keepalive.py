"""Keep-alive tests never access the real mount or Sync account."""
import contextlib
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import keepalive as k
import settings as s
import backend


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.schedule = k.KeepAliveSchedule(lambda: self.now)
        self.settings = s.Settings()
        self.schedule.configure(self.settings)
        self.online = Mock(state='online', stale_mount=False)
        self.offline = Mock(state='auth', stale_mount=True)

    def test_default_enabled_sixty_seconds(self):
        self.assertTrue(self.settings.keepalive_enabled)
        self.assertEqual(self.settings.keepalive_interval_seconds, 60)
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.now += 59
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.now += 1
        self.assertEqual(self.schedule.claim(self.online, False), self.schedule.generation)

    def test_no_overlap_and_full_delay_after_completion(self):
        self.schedule.claim(self.online, False)
        self.now += 60; generation = self.schedule.claim(self.online, False)
        self.now += 1000
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.assertTrue(self.schedule.complete(generation, k.KeepAliveResult('success','ok','12:00')))
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.now += 60
        self.assertIsNotNone(self.schedule.claim(self.online, False))

    def test_offline_and_busy_pause_and_reconnect_waits_interval(self):
        self.schedule.claim(self.online, False)
        self.now += 100
        self.assertIsNone(self.schedule.claim(self.offline, False))
        self.assertIsNone(self.schedule.claim(self.online, True))
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.now += 60
        self.assertIsNotNone(self.schedule.claim(self.online, False))

    def test_disable_and_interval_configuration_invalidate_result(self):
        self.schedule.claim(self.online, False)
        self.now += 60; generation = self.schedule.claim(self.online, False)
        self.schedule.configure(replace(self.settings, keepalive_enabled=False))
        self.assertFalse(self.schedule.complete(generation, k.KeepAliveResult('success','old','12:00')))
        self.now += 9999
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.assertEqual(self.schedule.description(self.online, False), 'Disabled')
        self.schedule.configure(replace(self.settings, keepalive_interval_seconds=300))
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.now += 299
        self.assertIsNone(self.schedule.claim(self.online, False))
        self.now += 1
        self.assertIsNotNone(self.schedule.claim(self.online, False))

    def test_pause_supersedes_pending(self):
        self.schedule.claim(self.online, False); self.now += 60
        generation = self.schedule.claim(self.online, False)
        self.schedule.pause()
        self.assertFalse(self.schedule.complete(generation, k.KeepAliveResult('success','old','12:00')))

    def test_no_catchup_storm_after_sleep(self):
        self.schedule.claim(self.online, False)
        self.now += 100000
        self.assertIsNotNone(self.schedule.claim(self.online, False))
        self.assertIsNone(self.schedule.claim(self.online, False))


class PingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / 'rclone.conf'
        self.settings = s.Settings(rclone_config=str(self.config))
        self.history = patch.dict(os.environ, {'XDG_CONFIG_HOME': self.temp.name})
        self.history.start()
        self.cancel = threading.Event()
        self.online = Mock(state='online', stale_mount=False)
        self.config.write_text('[sync-webdav]\ntype=webdav\nurl=http://127.0.0.1:4918\nuser=sync\npass=obscured\n')
        self.config.chmod(0o600)

    def tearDown(self):
        self.history.stop()
        self.temp.cleanup()

    def test_only_connected_reads_under_shared_operation_lock(self):
        with patch.object(k,'operation_lock',return_value=contextlib.nullcontext()) as lock, \
             patch.object(k,'read_remote') as read:
            result = k.ping(self.settings,self.cancel,status_reader=lambda _: self.online)
        self.assertEqual(result.state,'success')
        lock.assert_called_once_with(shared=True)
        read.assert_called_once()

    def test_disabled_cancelled_and_offline_do_not_read(self):
        with patch.object(k,'operation_lock',return_value=contextlib.nullcontext()), patch.object(k,'read_remote') as read:
            self.assertEqual(k.ping(replace(self.settings,keepalive_enabled=False),self.cancel).state,'skipped')
            self.cancel.set()
            self.assertEqual(k.ping(self.settings,self.cancel).state,'cancelled')
            self.cancel.clear()
            self.assertEqual(k.ping(self.settings,self.cancel,status_reader=lambda _:Mock(state='auth',stale_mount=True)).state,'skipped')
        read.assert_not_called()

    def test_no_unintended_remote_or_broad_permission_config(self):
        self.config.write_text('[sync-webdav]\ntype=webdav\nurl=https://unrelated.example\nuser=sync\npass=secret\n')
        with self.assertRaises(backend.OperationError):k.ensure_local_remote(self.settings)
        self.config.chmod(0o644)
        with self.assertRaises(backend.OperationError):k.ensure_local_remote(self.settings)

    def test_errors_never_leak_credentials_or_cli_output(self):
        with patch.object(k,'operation_lock',return_value=contextlib.nullcontext()), \
             patch.object(k,'read_remote',side_effect=backend.OperationError('Password: secret')):
            result=k.ping(self.settings,self.cancel,status_reader=lambda _:self.online)
        self.assertEqual(result.state,'failed')
        self.assertNotIn('secret',result.message)

    def test_command_is_shallow_read_with_discarded_output_and_clean_environment(self):
        child=Mock();child.poll.return_value=0;child.returncode=0
        with patch.object(k.subprocess,'Popen',return_value=child) as popen, \
             patch.object(k,'terminate_group'), patch.dict(os.environ,{'RCLONE_CONFIG_SYNC_WEBDAV_URL':'https://wrong.example','RCLONE_LOG_FILE':'/tmp/leak'}):
            k.read_remote(self.settings,self.cancel)
        args=popen.call_args.args[0]
        self.assertEqual(args[1:3],['lsf','sync-webdav:/'])
        self.assertIn('--max-depth=1',args)
        self.assertNotIn('--recursive',args)
        self.assertEqual(popen.call_args.kwargs['stdout'],subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs['stderr'],subprocess.DEVNULL)
        self.assertFalse(any(key.startswith('RCLONE_') for key in popen.call_args.kwargs['env']))

    def test_cancel_terminates_owned_child_group(self):
        child=Mock();child.poll.return_value=None
        self.cancel.set()
        with patch.object(k.subprocess,'Popen') as popen:
            with self.assertRaises(k.Cancelled):k.read_remote(self.settings,self.cancel)
            popen.assert_not_called()
        self.cancel.clear()
        with patch.object(k.subprocess,'Popen',return_value=child), \
             patch.object(k,'terminate_group') as terminate, patch.object(self.cancel,'wait',return_value=True):
            with self.assertRaises(k.Cancelled):k.read_remote(self.settings,self.cancel)
        terminate.assert_called_once_with(child)

    def test_shared_lock_does_not_mark_keepalive_as_recovery(self):
        with patch.dict(os.environ,{'XDG_RUNTIME_DIR':self.temp.name}):
            with backend.operation_lock(shared=True):
                self.assertFalse(backend.operation_busy())
                with self.assertRaises(backend.OperationError):
                    with backend.operation_lock():pass
            with backend.operation_lock():self.assertTrue(backend.operation_busy())


class SettingsTests(unittest.TestCase):
    def test_legacy_missing_fields_default_enabled(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,{'XDG_CONFIG_HOME':temporary}), patch.object(s,'HOME',Path(temporary)):
            path=s.settings_path();path.parent.mkdir(parents=True)
            data=s.Settings(mount_point=str(Path(temporary)/'drive')).to_dict();data.pop('keepalive_enabled');data.pop('keepalive_interval_seconds')
            path.write_text(json.dumps(data))
            settings=s.load_settings()
            self.assertTrue(settings.keepalive_enabled)
            self.assertEqual(settings.keepalive_interval_seconds,60)
            s.save_settings(replace(settings,keepalive_enabled=False,keepalive_interval_seconds=120))
            loaded=s.load_settings()
            self.assertFalse(loaded.keepalive_enabled)
            self.assertEqual(loaded.keepalive_interval_seconds,120)

    def test_validation(self):
        for value in [0,29,3601,True,'60',60.5]:
            with self.assertRaises(ValueError):replace(s.Settings(),keepalive_interval_seconds=value).validate()
        with self.assertRaises(ValueError):replace(s.Settings(),keepalive_enabled='yes').validate()


if __name__=='__main__': unittest.main()
