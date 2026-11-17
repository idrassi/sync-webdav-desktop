import contextlib
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

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

    def test_signin_success_order(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "sync-webdav"
            binary.touch(mode=0o700)
            calls = []
            def login():
                calls.append("login")
                return 0
            with patch.object(b, "WEBDAV_BIN", binary), \
                 patch.object(b, "operation_lock", contextlib.nullcontext), \
                 patch.object(b, "change_services", side_effect=lambda action: calls.append(action)), \
                 patch.object(b, "verify_mount", return_value="ok"), \
                 patch("mount_safety.cleanup_stale_mount"), patch("setup_engine.enable_failure_logging"):
                self.assertEqual(b.sign_in_restore(login=login), "ok")
            self.assertEqual(calls, ["stop", "login", "start"])

    def test_signin_cancel_does_not_start(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "sync-webdav"
            binary.touch(mode=0o700)
            with patch.object(b, "WEBDAV_BIN", binary), \
                 patch.object(b, "operation_lock", contextlib.nullcontext), \
                 patch.object(b, "change_services") as change, \
                 patch("mount_safety.cleanup_stale_mount"), patch("setup_engine.enable_failure_logging"):
                with self.assertRaises(b.OperationError):
                    b.sign_in_restore(login=lambda: 1)
                change.assert_called_once_with("stop")

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

    def test_interrupted_login_child_is_stopped_before_unlock(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "sync-webdav"
            binary.touch(mode=0o700)
            with patch.object(b, "WEBDAV_BIN", binary), \
                 patch.object(b, "operation_lock", contextlib.nullcontext), \
                 patch.object(b, "change_services") as change, \
                 patch("subprocess.Popen") as popen, \
                 patch("mount_safety.cleanup_stale_mount"), patch("setup_engine.enable_failure_logging"):
                child = popen.return_value
                child.wait.side_effect = [KeyboardInterrupt(), 0]
                with self.assertRaises(KeyboardInterrupt):
                    b.sign_in_restore()
                child.terminate.assert_called_once()
                self.assertEqual(child.wait.call_count, 2)
                change.assert_called_once_with("stop")
                self.assertEqual(popen.call_args.args[0], [str(binary), "login", "--profile", "default"])
                self.assertEqual(popen.call_args.kwargs, {})  # Inherits terminal; no pipes.

    def test_runtime_lock_and_private_results(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            self.assertFalse(b.operation_busy())
            with b.operation_lock():
                self.assertTrue(b.operation_busy())
                with self.assertRaises(b.OperationError):
                    with b.operation_lock():
                        pass
            self.assertFalse(b.operation_busy())
            operation = str(uuid.uuid4())
            b.write_result(operation, "error", "password=secret")
            path = b.result_path(operation)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret", path.read_text())
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(ValueError):
                b.result_path("../../bad")

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

    def test_terminal_arguments_not_shell_commands(self):
        operation = str(uuid.uuid4())
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/ptyxis" if name == "ptyxis" else None):
            args = b.recovery_command(operation)
        self.assertIn("--standalone", args)
        self.assertEqual(args[-1], operation)
        self.assertNotIn("sh", args)
        self.assertIn(str(b.APP_DIR / "recovery.py"), args)

    def test_subprocess_timeout_is_actionable(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 10)):
            with self.assertRaisesRegex(b.OperationError, "timed out"):
                b.run_command([b.SYSTEMCTL])

    def test_recovery_helper_with_mocked_services(self):
        spec = importlib.util.spec_from_file_location("recovery", b.APP_DIR / "recovery.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        operation = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}), \
             patch.object(sys, "argv", ["recovery.py", "--operation-id", operation]), \
             patch.object(helper, "sign_in_restore", return_value=type("Status", (), {"title": "Connected"})()), \
             patch("builtins.input", return_value=""):
            self.assertEqual(helper.main(), 0)
            result = json.loads(b.result_path(operation).read_text())
            self.assertEqual(result["state"], "success")


if __name__ == "__main__":
    unittest.main()
