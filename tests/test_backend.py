from dataclasses import replace
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend as b


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.online_webdav = b.Unit(b.SERVICES[0], "active", "running", "enabled", "success", "abc", "loaded")
        self.online_rclone = b.Unit(b.SERVICES[1], "active", "running", "enabled", "success", "xyz", "loaded")
        self.failed_webdav = replace(self.online_webdav, active="activating", sub="auto-restart", result="exit-code")
        self.stopped_rclone = replace(self.online_rclone, active="inactive", sub="dead")

    def test_parse_systemd_properties_with_reordered_fields(self):
        output = ("ActiveState=active\nId=sync-webdav.service\nSubState=running\nUnitFileState=enabled\n"
                  "LoadState=loaded\nInvocationID=abc\nResult=success\n\n"
                  "Id=sync-rclone.service\nActiveState=inactive\nSubState=dead\nUnitFileState=enabled\n")
        units = b.parse_units(output)
        self.assertTrue(units[b.SERVICES[0]].running)
        self.assertEqual(units[b.SERVICES[1]].active, "inactive")
        self.assertEqual(units[b.SERVICES[0]].invocation, "abc")

    def test_missing_units_and_journal_garbage(self):
        self.assertEqual(b.parse_units("")[b.SERVICES[0]].active, "unknown")
        self.assertEqual(b.parse_journal('junk\n[]\n{"MESSAGE": [1,2]}\n'), [])

    def test_auth_detection_ignores_old_errors_when_running(self):
        entries = [{"MESSAGE": "Auth rejected: revoked.", "_SYSTEMD_USER_UNIT": b.SERVICES[0]}]
        self.assertFalse(b.auth_rejected(entries, self.online_webdav))
        self.assertTrue(b.auth_rejected(entries, self.failed_webdav))
        entries.append({"MESSAGE": "Sync WebDAV Server v1.2.1", "_SYSTEMD_USER_UNIT": b.SERVICES[0]})
        self.assertTrue(b.auth_rejected(entries, self.failed_webdav))

    def test_different_failure_supersedes_auth(self):
        entries = [{"MESSAGE": "Auth rejected: revoked.", "_SYSTEMD_USER_UNIT": b.SERVICES[0]},
                   {"MESSAGE": "Address already in use", "_SYSTEMD_USER_UNIT": b.SERVICES[0]}]
        self.assertFalse(b.auth_rejected(entries, self.failed_webdav))

    def test_auth_from_rclone_is_not_cloud_session_failure(self):
        entries = [{"MESSAGE": "Auth rejected", "_SYSTEMD_USER_UNIT": b.SERVICES[1]}]
        self.assertFalse(b.auth_rejected(entries, self.failed_webdav))

    def test_status_requires_services_mount_and_http(self):
        self.assertEqual(b.classify(self.online_webdav, self.online_rclone, True, 401, False)[0], "online")
        self.assertEqual(b.classify(self.online_webdav, self.online_rclone, False, 401, False)[0], "offline")
        self.assertEqual(b.classify(self.online_webdav, self.online_rclone, True, None, False)[0], "offline")
        self.assertEqual(b.classify(self.online_webdav, self.online_rclone, True, 500, False)[0], "offline")
        self.assertEqual(b.classify(self.failed_webdav, self.stopped_rclone, False, None, True)[0], "auth")

    def test_secret_redaction(self):
        raw = ('password: hunter2\npass = secret\n{"refresh_token": "really-secret"}\n'
               'Authorization: Bearer bearer-secret\nCookie: a=cookie-secret\n'
               'http://sync:secret@localhost:4918/\nhttps://login.sync.com/?code=auth-secret\nuser@example.com')
        redacted = b.redact(raw)
        for secret in ['hunter2', 'really-secret', 'bearer-secret', 'cookie-secret',
                       'sync:secret', 'auth-secret', 'user@example.com']:
            self.assertNotIn(secret, redacted)
        self.assertNotIn('pass = secret', redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_journal_format_handles_invalid_timestamp(self):
        text = b.format_journal([{"MESSAGE": "password=secret", "__REALTIME_TIMESTAMP": "invalid"}])
        self.assertIn("--:--:--", text)
        self.assertNotIn("secret", text)

    def test_only_allowed_service_actions_and_stop_order(self):
        with patch.object(b, "run_command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            b.change_services("stop")
            self.assertEqual(run.call_args.args[0][-2:], list(b.SERVICES[::-1]))
            b.change_services("start")
            self.assertEqual(run.call_args.args[0][-2:], list(b.SERVICES))
            with self.assertRaises(ValueError):
                b.change_services("disable")

    def test_restart_serialized_and_verified(self):
        with patch("setup_engine.restart", return_value="verified") as restart:
            self.assertEqual(b.restart_services(), "verified")
            self.assertEqual(restart.call_count, 1)
            self.assertFalse(restart.call_args.args[3])

    def test_live_snapshot_normalizes_rclone_root_source(self):
        systemd = ("Id=sync-webdav.service\nActiveState=active\nSubState=running\nLoadState=loaded\n\n"
                   "Id=sync-rclone.service\nActiveState=active\nSubState=running\nLoadState=loaded\n")
        for source, expected in [("sync-webdav:", True), ("sync-webdav:/", True),
                                 ("other-remote:", False)]:
            def run(args, **kwargs):
                output = systemd if args[0] == b.SYSTEMCTL else f"fuse.rclone {source}\n" if "findmnt" in args[0] else ""
                return subprocess.CompletedProcess(args, 0, output, "")
            with patch.object(b, "run_command", side_effect=run), \
                 patch.object(b, "operation_busy", return_value=False), \
                 patch("http.client.HTTPConnection") as connection:
                connection.return_value.getresponse.return_value.status = 401
                snapshot = b.collect_snapshot()
                self.assertEqual(snapshot.mounted, expected, source)
                self.assertEqual(snapshot.state, "online" if expected else "offline", source)

    def test_snapshot_distinguishes_probe_timeout_from_other_http_failures(self):
        from settings import Settings
        services = ("Id=sync-webdav.service\nActiveState=active\n\n"
                    "Id=sync-rclone.service\nActiveState=active\n")

        def run(args, **kwargs):
            output = services if args[0] == b.SYSTEMCTL else "fuse.rclone sync-webdav:\n" if "findmnt" in args[0] else ""
            return subprocess.CompletedProcess(args, 0, output, "")

        for error, timed_out in [(socket.timeout(), True), (ConnectionRefusedError(), False)]:
            with self.subTest(error=error), patch.object(b, "run_command", side_effect=run), \
                 patch.object(b, "operation_busy", return_value=False), \
                 patch("diagnostics.daemon_log_tail", return_value=""), \
                 patch("activity_history.report", side_effect=ValueError), \
                 patch("http.client.HTTPConnection") as http:
                connection = http.return_value
                connection.getresponse.side_effect = error
                snapshot = b.collect_snapshot(Settings())
                connection.close.assert_called_once()
                self.assertEqual(snapshot.state, "offline")
                self.assertIsNone(snapshot.http_status)
                self.assertEqual(snapshot.as_dict()["http_timed_out"], timed_out)
                self.assertEqual(snapshot.webdav_delayed, timed_out)

    def test_runtime_lock_is_private_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            self.assertFalse(b.operation_busy())
            with b.operation_lock():
                self.assertTrue(b.operation_busy())
                with self.assertRaises(b.OperationError):
                    with b.operation_lock():
                        pass
            self.assertFalse(b.operation_busy())
            lock = b.private_runtime_dir() / "recovery.lock"
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            self.assertEqual(lock.parent.stat().st_mode & 0o777, 0o700)

    def test_autostart_only_changes_own_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            other = Path(directory) / "autostart/megasync.desktop"
            other.parent.mkdir()
            other.write_text("keep me")
            b.set_autostart(True)
            self.assertIn("--tray", b.autostart_path().read_text())
            b.set_autostart(False)
            self.assertFalse(b.autostart_path().exists())
            self.assertEqual(other.read_text(), "keep me")

    def test_subprocess_timeout_is_actionable(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 10)):
            with self.assertRaisesRegex(b.OperationError, "timed out"):
                b.run_command([b.SYSTEMCTL])

if __name__ == "__main__":
    unittest.main()
