"""All mount/service/process actions use fixture data or mocked helper commands."""
from dataclasses import replace
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
import mount_safety as m
from settings import Settings


STOPPED = "LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n"
FAILED = "LoadState=loaded\nActiveState=failed\nSubState=failed\nMainPID=0\nControlPID=0\n"


def escaped(value):
    for char, octal in [("\\", "\\134"), (" ", "\\040"), ("\t", "\\011"), ("\n", "\\012")]:
        value = value.replace(char, octal)
    return value


def mount_line(target, mount_id=41, fstype="fuse.rclone", source="sync-webdav:",
               owner=None, root="/", options=None):
    owner = os.getuid() if owner is None else owner
    options = options or f"rw,user_id={owner},group_id={os.getgid()}"
    return (f"{mount_id} 20 0:38 {escaped(root)} {escaped(str(target))} rw,nosuid,nodev "
            f"shared:8 master:1 - {fstype} {escaped(source)} {options}\n")


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.target = self.base / "Sync drive"
        self.target.mkdir()
        self.settings = Settings(mount_point=str(self.target))
        self.proc = self.base / "proc"
        (self.proc / "self").mkdir(parents=True)
        self.mountinfo = self.proc / "self/mountinfo"
        self.mountinfo.write_text(mount_line(self.target))
        self.addCleanup(patch.stopall)
        patch.object(m, "PROC_ROOT", self.proc).start()
        # Every test defaults to refusing all real command execution.
        self.run = patch.object(m, "run_command", side_effect=AssertionError("Unexpected command")).start()
        self.find_fusermount = m._find_fusermount
        self.helper = patch.object(m, "_find_fusermount", return_value="/usr/bin/fusermount3").start()

    def process(self, args, uid=None, pid=101, state="S (sleeping)", cwd=None):
        directory = self.proc / str(pid)
        directory.mkdir()
        uid = os.getuid() if uid is None else uid
        (directory / "status").write_text(
            f"Name:\trclone\nState:\t{state}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (directory / "cmdline").write_bytes(
            b"" if not args else b"\0".join(os.fsencode(arg) for arg in args) + b"\0")
        if cwd:
            (directory / "cwd").symlink_to(cwd)
        return directory

    def commands(self, normal_code=0, normal_error="", lazy_code=0, lazy_error="",
                 service=STOPPED, normal_effect=None, lazy_effect=None):
        def run(args, timeout):
            if args[0] == m.SYSTEMCTL:
                self.assertEqual(args[1:4], ["--user", "show", "sync-rclone.service"])
                properties = args[-1]
                for field in ("ActiveState", "SubState", "MainPID", "ControlPID", "LoadState"):
                    self.assertIn(field, properties)
                self.assertEqual(timeout, 5)
                return subprocess.CompletedProcess(args, 0, service, "")
            self.assertEqual(args[0], "/usr/bin/fusermount3")
            self.assertEqual(args[-2:], ["--", str(self.target)])
            self.assertEqual(timeout, m.COMMAND_TIMEOUT)
            self.assertNotIn("-f", args)
            self.assertNotIn("sudo", args)
            if "-z" in args:
                code, error, effect = lazy_code, lazy_error, lazy_effect
                self.assertEqual(args[1:-2], ["-u", "-z"])
            else:
                code, error, effect = normal_code, normal_error, normal_effect
                self.assertEqual(args[1:-2], ["-u"])
            if effect:
                effect()
            elif code == 0:
                self.mountinfo.write_text("")
            return subprocess.CompletedProcess(args, code, "", error)
        self.run.side_effect = run
        return self.run

    def unmount_calls(self):
        return [call for call in self.run.call_args_list if call.args[0][0] != m.SYSTEMCTL]


class MountInspectionTests(FixtureCase):
    def test_exact_read_only_inspection(self):
        self.mountinfo.write_text(mount_line(self.base) + mount_line(str(self.target) + "-other"))
        with patch.object(os, "lstat", side_effect=AssertionError("Inspection must not stat")):
            self.assertIsNone(m.inspect_mount(self.settings))
        self.run.assert_not_called()
        self.helper.assert_not_called()
        self.mountinfo.write_text(mount_line(self.target))
        with patch.object(os, "lstat", side_effect=AssertionError("Inspection must not stat")):
            mount = m.inspect_mount(self.settings)
        self.assertEqual(mount.mount_id, 41)
        self.assertEqual(mount.target, str(self.target))
        self.run.assert_not_called()

    def test_parse_octal_paths_source_flags_and_super_owner(self):
        target = "/home/user/space tab\tline\nback\\040"
        mount = m.parse_mountinfo(mount_line(target, source="remote name:/", root="/root directory"))[0]
        self.assertEqual(mount.target, target)
        self.assertEqual(mount.root, "/root directory")
        self.assertEqual(mount.source, "remote name:/")
        self.assertEqual(mount.fstype, "fuse.rclone")
        self.assertEqual(mount.parent_id, 20)
        self.assertEqual(mount.device, "0:38")
        self.assertEqual(mount.mount_options, ("rw", "nosuid", "nodev"))
        self.assertEqual(mount.optional_fields, ("shared:8", "master:1"))
        self.assertIn(f"user_id={os.getuid()}", mount.super_options)
        self.assertEqual(mount.user_id, os.getuid())

    def test_snap_namespace_synthetic_root_is_valid_mountinfo(self):
        namespace = '12 20 0:4 mnt:[4026532954] /run/snapd/ns/example.mnt rw - nsfs nsfs rw\n'
        parsed = m.parse_mountinfo(namespace + mount_line(self.target))
        self.assertEqual(parsed[0].root, 'mnt:[4026532954]')
        self.mountinfo.write_text(namespace + mount_line(self.target))
        self.assertEqual(m.inspect_mount(self.settings).target, str(self.target))

    def test_missing_and_ambiguous_owner_is_not_guessed(self):
        for options in ("rw", "rw,user_id=oops", "rw,user_id=-1", "rw,user_id=",
                        "rw,user_id=0,user_id=0", "rw,user_id"):
            with self.subTest(options=options):
                self.assertIsNone(m.parse_mountinfo(mount_line(self.target, options=options))[0].user_id)
        # A similarly named option in per-mount flags is not the FUSE super owner.
        line = mount_line(self.target, options="rw").replace("rw,nosuid,nodev", "rw,user_id=0")
        self.assertIsNone(m.parse_mountinfo(line)[0].user_id)

    def test_stacked_inspection_uses_last_not_highest_id(self):
        self.mountinfo.write_text(mount_line(self.target, mount_id=99) + mount_line(self.target, mount_id=42))
        self.assertEqual(m.inspect_mount(self.settings).mount_id, 42)
        with self.assertRaisesRegex(backend.OperationError, "stacked"):
            m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.run.assert_not_called()

    def test_lexical_normalization_never_resolves_endpoint(self):
        lexical = replace(self.settings, mount_point=str(self.target) + "/./")
        with patch.object(Path, "resolve", side_effect=AssertionError("No realpath")):
            self.assertEqual(m.inspect_mount(lexical).target, str(self.target))

    def test_invalid_path_and_broken_mountinfo_fail_closed(self):
        for path in ("relative/drive", "/home/user/../drive", "//home/user/drive", "/drive\n"):
            with self.subTest(path=path), self.assertRaises(backend.OperationError):
                m.inspect_mount(replace(self.settings, mount_point=path))
        for line in ("broken", "1 0 0:1 / / rw - fuse.rclone remote:",
                     "0 0 0:1 / / rw - fuse.rclone remote: rw",
                     "1 0 invalid / / rw - fuse.rclone remote: rw",
                     "1 0 0:1 / relative rw - fuse.rclone remote: rw",
                     "1 0 0:1 / /rw rw - fuse.rclone remote: rw extra",
                     r"1 0 0:1 / /bad\000 rw - fuse.rclone remote: rw",
                     r"1 0 0:1 / /bad\777 rw - fuse.rclone remote: rw"):
            with self.subTest(line=line), self.assertRaises(backend.OperationError):
                m.parse_mountinfo(line)
        self.mountinfo.unlink()
        with self.assertRaisesRegex(backend.OperationError, "state is unknown"):
            m.inspect_mount(self.settings)


class CleanupTests(FixtureCase):
    def test_no_mount_is_noop_even_if_lazy_allowed(self):
        self.mountinfo.write_text(mount_line(self.base))
        for allow in (False, True):
            self.assertFalse(m.cleanup_stale_mount(self.settings, allow_lazy=allow))
        self.run.assert_not_called()
        self.helper.assert_not_called()

    def test_normal_unmount_is_first_and_rechecks_absence(self):
        self.commands()
        progress = []
        self.assertTrue(m.cleanup_stale_mount(self.settings, allow_lazy=True, progress=progress.append))
        self.assertIsNone(m.inspect_mount(self.settings))
        self.assertEqual(len(self.unmount_calls()), 1)
        self.assertEqual(len(progress), 1)

    def test_source_root_normalization_only(self):
        for source in ("sync-webdav:", "sync-webdav:/", "sync-webdav:////"):
            with self.subTest(source=source):
                self.mountinfo.write_text(mount_line(self.target, source=source))
                self.commands(service=FAILED)
                self.assertTrue(m.cleanup_stale_mount(self.settings))

    def test_foreign_filesystem_source_and_owner_never_touched(self):
        for values in ({"fstype": "ext4"}, {"fstype": "fuse.sshfs"},
                       {"source": "other:"}, {"source": "sync-webdav:subfolder/"},
                       {"source": "sync-webdav:/../"}, {"source": "sync-webdav: /"},
                       {"owner": os.getuid() + 1}, {"options": "rw"},
                       {"options": f"rw,user_id={os.getuid()},user_id={os.getuid()}"}):
            with self.subTest(values=values):
                self.mountinfo.write_text(mount_line(self.target, **values))
                with self.assertRaises(backend.OperationError):
                    m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.run.assert_not_called()

    def test_busy_requires_actionable_explicit_consent(self):
        self.commands(normal_code=1, normal_error="fusermount3: failed to unmount: Device or resource busy")
        with self.assertRaises(m.StaleMountBusy) as raised:
            m.cleanup_stale_mount(self.settings)
        self.assertIsInstance(raised.exception, backend.OperationError)
        self.assertIn("Nautilus", str(raised.exception))
        self.assertIn("file handles", str(raised.exception))
        self.assertIn("explicit lazy-detach confirmation", str(raised.exception))
        self.assertEqual(len(self.unmount_calls()), 1)
        self.assertIsNotNone(m.inspect_mount(self.settings))

    def test_explicit_lazy_only_after_busy_normal_attempt(self):
        self.commands(normal_code=1, normal_error="Device or resource busy")
        self.assertTrue(m.cleanup_stale_mount(self.settings, allow_lazy=True))
        calls = self.run.call_args_list
        self.assertEqual([call.args[0][0] for call in calls],
                         [m.SYSTEMCTL, "/usr/bin/fusermount3", m.SYSTEMCTL, "/usr/bin/fusermount3"])
        self.assertEqual(self.unmount_calls()[1].args[0],
                         ["/usr/bin/fusermount3", "-u", "-z", "--", str(self.target)])
        self.assertIsNone(m.inspect_mount(self.settings))

    def test_nonbusy_failure_never_lazily_detaches_and_redacts(self):
        self.commands(normal_code=1, normal_error="Permission denied; password=hunter2")
        with self.assertRaises(backend.OperationError) as raised:
            m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.assertNotIsInstance(raised.exception, m.StaleMountBusy)
        self.assertNotIn("hunter2", str(raised.exception))
        self.assertEqual(len(self.unmount_calls()), 1)

    def test_empty_error_is_reported_and_lazy_failure_is_reported(self):
        self.commands(normal_code=1)
        with self.assertRaisesRegex(backend.OperationError, "Normal.*failed"):
            m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.commands(normal_code=1, normal_error="busy", lazy_code=1, lazy_error="Permission denied")
        with self.assertRaisesRegex(backend.OperationError, "Permission denied"):
            m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.assertIsNotNone(m.inspect_mount(self.settings))

    def test_success_with_remaining_or_replacement_mount_is_not_success(self):
        for lazy in (False, True):
            for replacement in (41, 99):
                with self.subTest(lazy=lazy, replacement=replacement):
                    self.mountinfo.write_text(mount_line(self.target))
                    effect = lambda: self.mountinfo.write_text(mount_line(self.target, mount_id=replacement))
                    self.commands(normal_code=1 if lazy else 0, normal_error="busy" if lazy else "",
                                  normal_effect=None if lazy else effect, lazy_effect=effect if lazy else None)
                    with self.assertRaisesRegex(backend.OperationError, "entry remains or was replaced"):
                        m.cleanup_stale_mount(self.settings, allow_lazy=lazy)

    def test_mount_identity_changed_before_normal_attempt_is_refused(self):
        self.commands()
        def progress(_):
            self.mountinfo.write_text(mount_line(self.target, mount_id=99))
        with self.assertRaisesRegex(backend.OperationError, "identity changed"):
            m.cleanup_stale_mount(self.settings, progress=progress)
        self.assertEqual(self.unmount_calls(), [])

    def test_every_identity_change_after_busy_prevents_lazy(self):
        for change in ({"mount_id": 99}, {"fstype": "ext4"}, {"source": "other:"},
                       {"owner": os.getuid() + 1}, {"root": "/subdir"},
                       {"source": "sync-webdav:/"}):
            with self.subTest(change=change):
                self.mountinfo.write_text(mount_line(self.target))
                def changed():
                    self.mountinfo.write_text(mount_line(self.target, **change))
                self.run.reset_mock()
                self.commands(normal_code=1, normal_error="busy", normal_effect=changed)
                with self.assertRaisesRegex(backend.OperationError, "identity changed"):
                    m.cleanup_stale_mount(self.settings, allow_lazy=True)
                self.assertEqual(len(self.unmount_calls()), 1)

    def test_disappeared_or_stacked_after_busy_prevents_lazy(self):
        for replacement in ("", mount_line(self.target) + mount_line(self.target, mount_id=99)):
            with self.subTest(replacement=replacement):
                self.mountinfo.write_text(mount_line(self.target))
                self.run.reset_mock()
                self.commands(normal_code=1, normal_error="busy",
                              normal_effect=lambda: self.mountinfo.write_text(replacement))
                with self.assertRaises(backend.OperationError):
                    m.cleanup_stale_mount(self.settings, allow_lazy=True)
                self.assertEqual(len(self.unmount_calls()), 1)

    def test_live_owner_appearing_after_normal_busy_prevents_lazy(self):
        self.commands(normal_code=1, normal_error="busy", normal_effect=lambda: self.process(
            ["/usr/bin/rclone", "mount", "sync-webdav:", str(self.target)]))
        with self.assertRaisesRegex(backend.OperationError, "live same-user rclone"):
            m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.assertEqual(len(self.unmount_calls()), 1)

    def test_process_appearing_in_progress_callback_is_checked(self):
        self.commands()
        with self.assertRaisesRegex(backend.OperationError, "live same-user rclone"):
            m.cleanup_stale_mount(self.settings, progress=lambda _: self.process(
                ["rclone", "mount", "sync-webdav:", str(self.target)]))
        self.assertEqual(self.unmount_calls(), [])

    def test_boolean_confirmation_cannot_be_truthy_string(self):
        with self.assertRaisesRegex(backend.OperationError, "explicit boolean"):
            m.cleanup_stale_mount(self.settings, allow_lazy="yes")
        self.run.assert_not_called()

    def test_command_timeout_and_missing_binary_never_trigger_lazy(self):
        for error in (subprocess.TimeoutExpired("fusermount3", 10), FileNotFoundError("missing")):
            with self.subTest(error=error):
                self.run.reset_mock()
                with patch.object(backend.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, STOPPED, ""), error]):
                    self.run.side_effect = backend.run_command
                    with self.assertRaises(backend.OperationError):
                        m.cleanup_stale_mount(self.settings, allow_lazy=True)
                self.assertEqual(len(self.unmount_calls()), 1)


class OwnershipTests(FixtureCase):
    def test_service_must_be_known_stopped_and_have_no_control_pid(self):
        service_states = ["", STOPPED.replace("LoadState=loaded", "LoadState=not-found"),
                          STOPPED.replace("ActiveState=inactive", "ActiveState=unknown"),
                          STOPPED.replace("ActiveState=inactive\nSubState=dead", "ActiveState=active\nSubState=running"),
                          STOPPED.replace("ActiveState=inactive\nSubState=dead", "ActiveState=activating\nSubState=start"),
                          STOPPED.replace("SubState=dead", "SubState=auto-restart"),
                          STOPPED.replace("MainPID=0", "MainPID=123"),
                          STOPPED.replace("ControlPID=0", "ControlPID=321"),
                          STOPPED.replace("ControlPID=0", "ControlPID=unknown"),
                          STOPPED.replace("ControlPID=0\n", ""),
                          STOPPED + "MainPID=0\n", "not properties\n"]
        for service in service_states:
            with self.subTest(service=service):
                self.commands(service=service)
                with self.assertRaises(backend.OperationError):
                    m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.assertEqual(self.unmount_calls(), [])

    def test_service_command_failure_and_timeout_fail_closed(self):
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 1, STOPPED, "Access denied")
        with self.assertRaisesRegex(backend.OperationError, "service state"):
            m.cleanup_stale_mount(self.settings)
        self.run.side_effect = backend.OperationError("systemctl timed out")
        with self.assertRaisesRegex(backend.OperationError, "timed out"):
            m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])

    def test_same_user_live_rclone_args_and_command_paths_are_refused(self):
        for args in (["rclone", "mount", "sync-webdav:", str(self.target)],
                     ["/usr/bin/rclone", "mount", "sync-webdav:/", str(self.target), "--daemon"],
                     ["/opt/bin/rclone", "--config", "/config", "mount", "sync-webdav:", "--read-only", str(self.target)],
                     ["rclone", "mount", "--config=/config", "sync-webdav:", "--vfs-cache-mode", "writes", str(self.target)],
                     ["rclone", "cmount", "sync-webdav:", str(self.target)],
                     ["rclone", "mount", "sync-webdav:", str(self.target) + "/./"]):
            with self.subTest(args=args):
                directory = self.process(args)
                self.commands()
                with self.assertRaisesRegex(backend.OperationError, "live same-user rclone"):
                    m.cleanup_stale_mount(self.settings, allow_lazy=True)
                self.assertEqual(self.unmount_calls(), [])
                shutil_remove(directory)

    def test_relative_exact_target_is_detected_using_proc_cwd(self):
        self.process(["rclone", "mount", "sync-webdav:", "Sync drive"], cwd=self.base)
        self.commands()
        with self.assertRaisesRegex(backend.OperationError, "live same-user rclone"):
            m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])

    def test_unreadable_relative_rclone_cwd_fails_closed(self):
        self.process(["rclone", "mount", "sync-webdav:", "Sync drive"])
        self.commands()
        with self.assertRaisesRegex(backend.OperationError, "working directory"):
            m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])

    def test_other_process_remote_target_and_foreign_user_do_not_own_mount(self):
        self.process(["rclone", "mount", "other:", str(self.target)], pid=101)
        self.process(["rclone", "mount", "sync-webdav:", str(self.target) + "-other"], pid=102)
        self.process(["rclone", "copy", "sync-webdav:", str(self.target)], pid=103)
        self.process(["/usr/bin/python3", "script.py", "rclone", "mount", "sync-webdav:", str(self.target)], pid=104)
        foreign = self.process(["rclone", "mount", "sync-webdav:", str(self.target)], uid=os.getuid() + 1, pid=105)
        (foreign / "cmdline").unlink()  # Foreign cmdlines need not be accessible.
        self.commands()
        self.assertTrue(m.cleanup_stale_mount(self.settings))

    def test_foreign_proc_directory_does_not_require_restricted_metadata(self):
        directory = self.process(["rclone", "mount", "sync-webdav:", str(self.target)])
        real_stat, real_read = Path.stat, Path.read_text
        foreign_uid = os.getuid() + 1000
        def stat(path, *args, **kwargs):
            if path == directory:
                return SimpleNamespace(st_uid=foreign_uid)
            return real_stat(path, *args, **kwargs)
        def read(path, *args, **kwargs):
            if path == directory / "status":
                raise AssertionError("Must not read known foreign-user metadata")
            return real_read(path, *args, **kwargs)
        self.commands()
        with patch.object(Path, "stat", new=stat), patch.object(Path, "read_text", new=read):
            self.assertTrue(m.cleanup_stale_mount(self.settings))

    def test_root_owned_proc_directory_with_same_user_uid_still_blocks(self):
        directory = self.process(["rclone", "mount", "sync-webdav:", str(self.target)])
        real_stat = Path.stat
        def stat(path, *args, **kwargs):
            if path == directory:
                return SimpleNamespace(st_uid=0)
            return real_stat(path, *args, **kwargs)
        self.commands()
        with patch.object(Path, "stat", new=stat), self.assertRaisesRegex(
                backend.OperationError, "live same-user rclone"):
            m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])

    def test_empty_live_cmdline_and_missing_same_user_metadata_fail_closed(self):
        directory = self.process([])
        self.commands()
        with self.assertRaisesRegex(backend.OperationError, "empty live process"):
            m.cleanup_stale_mount(self.settings)
        (directory / "cmdline").unlink()
        with self.assertRaisesRegex(backend.OperationError, "Inaccessible process"):
            m.cleanup_stale_mount(self.settings)
        (directory / "status").unlink()
        with self.assertRaisesRegex(backend.OperationError, "Inaccessible process"):
            m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])

    def test_zombie_and_kernel_thread_are_positively_identified(self):
        self.process([], state="Z (zombie)")
        kernel = self.process([], pid=102)
        with (kernel / "status").open("a") as stream:
            stream.write("Kthread:\t1\n")
        self.commands()
        self.assertTrue(m.cleanup_stale_mount(self.settings))

    def test_invalid_uid_and_truncated_cmdline_fail_closed(self):
        directory = self.process(["rclone", "mount", "sync-webdav:", str(self.target)])
        self.commands()
        (directory / "status").write_text("Uid:\tinvalid\n")
        with self.assertRaisesRegex(backend.OperationError, "ownership"):
            m.cleanup_stale_mount(self.settings)
        (directory / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")
        (directory / "cmdline").write_bytes(b"rclone\0mount")
        with self.assertRaisesRegex(backend.OperationError, "Incomplete"):
            m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])

    def test_metadata_permission_error_even_unknown_owner_fails_closed(self):
        directory = self.process(["bash"])
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == directory / "status":
                raise PermissionError(errno.EACCES, "denied")
            return real_read(path, *args, **kwargs)
        self.commands()
        with patch.object(Path, "read_text", new=read), self.assertRaisesRegex(
                backend.OperationError, "Inaccessible process metadata"):
            m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])

    def test_process_that_disappears_during_scan_can_be_ignored(self):
        directory = self.process(["bash"])
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == directory / "status":
                shutil_remove(directory)
                raise FileNotFoundError(errno.ENOENT, "gone")
            return real_read(path, *args, **kwargs)
        self.commands()
        with patch.object(Path, "read_text", new=read):
            self.assertTrue(m.cleanup_stale_mount(self.settings))

    def test_proc_enumeration_failure_fails_closed(self):
        self.commands()
        with patch.object(Path, "iterdir", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaisesRegex(backend.OperationError, "scan process"):
                m.cleanup_stale_mount(self.settings)
        self.assertEqual(self.unmount_calls(), [])


class PathAndHelperTests(FixtureCase):
    def test_symlink_endpoint_and_ancestor_are_rejected(self):
        self.target.rmdir()
        self.target.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(backend.OperationError, "symbolic link"):
            m.cleanup_stale_mount(self.settings)
        self.target.unlink()
        real = self.base / "real"
        real.mkdir()
        (real / "drive").mkdir()
        self.target.symlink_to(real, target_is_directory=True)
        self.settings = replace(self.settings, mount_point=str(self.target / "drive"))
        self.mountinfo.write_text(mount_line(self.target / "drive"))
        with self.assertRaisesRegex(backend.OperationError, "symbolic link"):
            m.cleanup_stale_mount(self.settings)
        self.run.assert_not_called()

    def test_dead_endpoint_enotconn_only_ancestors_are_required(self):
        real_lstat = os.lstat
        visits = []
        def lstat(path):
            visits.append(Path(path))
            if Path(path) == self.target:
                raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")
            return real_lstat(path)
        self.commands()
        with patch.object(os, "lstat", side_effect=lstat), patch.object(
                Path, "resolve", side_effect=AssertionError("Must not traverse dead FUSE")):
            self.assertTrue(m.cleanup_stale_mount(self.settings))
        self.assertLess(visits.index(self.target.parent), visits.index(self.target))

    def test_other_endpoint_errors_and_dead_ancestor_fail_closed(self):
        real_lstat = os.lstat
        for path, error in ((self.target, errno.EACCES), (self.target, errno.ENOENT),
                            (self.target.parent, errno.ENOTCONN)):
            with self.subTest(path=path, error=error):
                def lstat(probe):
                    if Path(probe) == path:
                        raise OSError(error, "unsafe")
                    return real_lstat(probe)
                with patch.object(os, "lstat", side_effect=lstat), self.assertRaises(backend.OperationError):
                    m.cleanup_stale_mount(self.settings)
        self.run.assert_not_called()

    def test_symlink_appearing_after_busy_prevents_lazy(self):
        def replace_path():
            self.target.rmdir()
            self.target.symlink_to(self.base, target_is_directory=True)
        self.commands(normal_code=1, normal_error="busy", normal_effect=replace_path)
        with self.assertRaisesRegex(backend.OperationError, "symbolic link"):
            m.cleanup_stale_mount(self.settings, allow_lazy=True)
        self.assertEqual(len(self.unmount_calls()), 1)

    def test_distribution_helper_selection_and_fallback_exclude_user_path(self):
        with patch.object(os.path, "isfile", return_value=True), patch.object(os, "access", return_value=True), \
                patch.object(m.shutil, "which") as which:
            self.assertEqual(self.find_fusermount(), "/usr/bin/fusermount3")
            which.assert_not_called()
        with patch.object(os.path, "isfile", return_value=False), \
                patch.object(m.shutil, "which", side_effect=[None, "/bin/fusermount"]) as which:
            self.assertEqual(self.find_fusermount(), "/bin/fusermount")
            self.assertEqual(which.call_args_list[0].kwargs, {"path": os.defpath})
            self.assertEqual(which.call_args_list[1].kwargs, {"path": os.defpath})
        with patch.object(os.path, "isfile", return_value=False), \
                patch.object(m.shutil, "which", return_value=None):
            with self.assertRaisesRegex(backend.OperationError, "No fusermount"):
                self.find_fusermount()


def shutil_remove(directory):
    # All calls are restricted to temporary fixture process directories.
    import shutil
    shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
