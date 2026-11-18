"""Offline RPM packaging tests. No RPM tools, root, GI or network required."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rpm_packaging", ROOT / "packaging/build_rpm.py")
rpm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rpm
SPEC.loader.exec_module(rpm)


class RPMPackagingTests(unittest.TestCase):
    def test_python38_syntax(self):
        # Future annotations allow helpers to use modern annotation expressions.
        ast.parse((ROOT / "packaging/build_rpm.py").read_text(), feature_version=8)

    def test_version_literals_and_optional_version(self):
        self.assertEqual(rpm.installed_version(supplied="1.0.0"), "1.0.0")
        self.assertEqual(rpm.artifact_name("1.0.0"), "sync-webdav-desktop-1.0.0-1.noarch.rpm")
        for version in ("v1.0.0", "1.0.0-1", "1.0.1", "01.0.0", "1.0.0\n%post", "../1.0.0"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                rpm.installed_version(supplied=version)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "install.py").write_text('raise RuntimeError("must not execute")\nVERSION = "1.0.0"\n')
            (root / "settings.py").write_text('raise RuntimeError("must not execute")\nVERSION = "1.0.0"\n')
            self.assertEqual(rpm.installed_version(root), "1.0.0")
            (root / "settings.py").write_text('VERSION = "2.0.0"\n')
            with self.assertRaisesRegex(ValueError, "settings.py"):
                rpm.installed_version(root)
            (root / "install.py").write_text('VERSION = str(1)\n')
            with self.assertRaisesRegex(ValueError, "literal"):
                rpm.installed_version(root)

    def test_epoch_bounds(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rpm.source_epoch(), rpm.BUILD.DEFAULT_EPOCH)
        for value in ("0", "4294967295", "1767225600"):
            with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": value}):
                self.assertEqual(rpm.source_epoch(), int(value))
        for value in ("-1", "4294967296", "nonsense", "1.5"):
            with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": value}), self.assertRaises(ValueError):
                rpm.source_epoch()

    def fixture(self, root):
        for path in rpm.INSTALLER.runtime_files(ROOT):
            destination = root / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        for name in ("README.md", "LICENSE", "docs/DISTRIBUTION.md", "docs/CHANGELOG.md", "docs/RPM.md",
                     "packaging/sync-webdav-desktop", "packaging/" + rpm.APP_ID + ".desktop"):
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)

    def test_payload_dynamic_allowlist_paths_and_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "future_module.py").write_text("# public runtime module\n")
            (root / "icons/future-icon.png").write_bytes(b"public icon fixture")
            for name in ("install.py", "test_leak.py", "credentials.py", "private.py", "token.py",
                         "secrets.json", ".env", "rclone.conf", "__pycache__/cache.pyc", "vendor/sync-webdav",
                         "runtime/result.json", "tests/test_fixture.py", "dist/stale.rpm"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"DO NOT DISTRIBUTE")
            with mock.patch.object(rpm.INSTALLER, "runtime_files", wraps=rpm.INSTALLER.runtime_files) as discover:
                files = rpm.payload_files(root)
                discover.assert_called_once_with(root)
            self.assertIn("/usr/share/sync-webdav-desktop/future_module.py", files)
            self.assertIn("/usr/share/sync-webdav-desktop/icons/future-icon.png", files)
            self.assertEqual(files["/usr/bin/sync-webdav-desktop"].mode, 0o755)
            self.assertTrue(all(member.mode == 0o644 for name, member in files.items()
                                if name != "/usr/bin/sync-webdav-desktop"))
            self.assertTrue(all(b"DO NOT DISTRIBUTE" not in member.data for member in files.values()))
            self.assertEqual(files["/usr/bin/sync-webdav-desktop"].data,
                             (ROOT / "packaging/sync-webdav-desktop").read_bytes())
            self.assertEqual(files["/usr/share/applications/" + rpm.APP_ID + ".desktop"].data,
                             (ROOT / "packaging" / (rpm.APP_ID + ".desktop")).read_bytes())
            self.assertIn("/usr/share/licenses/sync-webdav-desktop/LICENSE", files)
            manifest = rpm.file_manifest(files)
            self.assertIn("%dir /usr/share/sync-webdav-desktop/icons", manifest)
            self.assertIn("%attr(0755,root,root) /usr/bin/sync-webdav-desktop", manifest)
            self.assertNotIn("%dir /usr/bin\n", manifest)
            self.assertIn("%license /usr/share/licenses/sync-webdav-desktop/LICENSE", manifest)

    def test_reject_symlinked_inputs(self):
        for name in ("app.py", "icons", "docs", "packaging", "LICENSE"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root)
                source = root / name
                target = root / ("moved-" + source.name)
                source.rename(target)
                source.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symlink|regular files"):
                    rpm.payload_files(root)

    def test_spec_has_portable_fixed_dependencies_and_safety_controls(self):
        spec = rpm.render_spec("1.0.0", 1767225600, rpm.payload_files())
        self.assertNotIn("@VERSION@", spec)
        self.assertNotIn("%{?dist}", spec)
        for text in ("Version: 1.0.0\n", "Release: 1\n", "BuildArch: noarch\n", "License: MIT\n",
                     "AutoReqProv: no", "%global debug_package %{nil}",
                     "%global __os_install_post %{nil}", "%global _binary_payload w9.gzdio",
                     "%global clamp_mtime_to_source_date_epoch 1", "%global use_source_date_epoch_as_buildtime 1",
                     "(gtk3 >= 3.16 or typelib-1_0-Gtk-3_0 >= 3.16)"):
            self.assertIn(text, spec)
        for dependency in rpm.REQUIRES:
            self.assertIn("Requires: " + dependency + "\n", spec)
        for dependency in rpm.RECOMMENDS:
            self.assertIn("Recommends: " + dependency + "\n", spec)
        self.assertNotIn("BuildRequires:", spec)
        scripts = spec.split("%post\n")[1].split("%files\n")[0]
        self.assertEqual(scripts.count(rpm.CACHE_SCRIPT), 2)
        for text in ("systemctl", "sudo", "curl", "rclone", "/home/", "%pre\n", "pkexec"):
            self.assertNotIn(text, scripts)
        subprocess.run(["/bin/sh", "-n"], input=rpm.CACHE_SCRIPT.encode(), check=True)

    def test_source_staging_is_reproducible_and_only_install_assets(self):
        files = rpm.payload_files()
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory)
            archives = []
            for name in ("first", "second"):
                topdir = top / name
                topdir.mkdir()
                spec = rpm.stage_sources(topdir, "1.0.0", 123456, files)
                self.assertEqual(spec.read_text(), rpm.render_spec("1.0.0", 123456, files))
                archive = (topdir / "SOURCES/sync-webdav-desktop-1.0.0.tar.gz").read_bytes()
                archives.append(archive)
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                    members = tar.getmembers()
                    regular = {member.name: tar.extractfile(member).read() for member in members if member.isfile()}
                    self.assertEqual(regular, {"sync-webdav-desktop-1.0.0/payload" + name: member.data
                                               for name, member in files.items()})
                    self.assertTrue(all(member.uid == member.gid == 0 and member.mtime == 123456
                                        for member in members))
                    self.assertTrue(all(member.isfile() or member.isdir() for member in members))
            self.assertEqual(archives[0], archives[1])

    def query_responses(self, files):
        entries = ["{}\t{:o}\troot\troot\t\t{}".format(name, stat.S_IFREG | member.mode,
                    hashlib.sha256(member.data).hexdigest()) for name, member in sorted(files.items())]
        entries.extend("{}\t40755\troot\troot\t\t".format(name) for name in sorted(rpm.owned_directories(files)))
        return {
            rpm.METADATA_FORMAT: "sync-webdav-desktop\n1.0.0\n1\nnoarch\nMIT\ncpio\ngzip\n8\n",
            rpm.FILES_FORMAT: "\n".join(entries) + "\n",
            "--requires": "\n".join(rpm.REQUIRES + ("/bin/sh",) + tuple(sorted(rpm.RPMLIB_REQUIRES))) + "\n",
            "--recommends": "\n".join(rpm.RECOMMENDS) + "\n",
            "--scripts": "postinstall scriptlet (using /bin/sh):\n" + rpm.CACHE_SCRIPT +
                         "\npostuninstall scriptlet (using /bin/sh):\n" + rpm.CACHE_SCRIPT + "\n",
        }

    def fake_query(self, responses):
        def query(command, **kwargs):
            if command[2] == "--checksig":
                self.assertEqual(command[:5], ["/mock/rpm", "--noplugins", "--checksig", "--nosignature", "--"])
                return "digests OK\n"
            self.assertEqual(command[:3], ["/mock/rpm", "--noplugins", "-qp"])
            self.assertEqual(command[-2], "--")
            key = command[4] if command[3] == "--qf" else command[3]
            return responses.get(key, "")
        return query

    def test_checker_metadata_payload_dependencies_and_scriptlets(self):
        files = rpm.payload_files()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / rpm.artifact_name("1.0.0")
            artifact.write_bytes(b"mock RPM")
            responses = self.query_responses(files)
            with mock.patch.object(rpm, "run", side_effect=self.fake_query(responses)) as query:
                rpm.check_artifact(artifact, "1.0.0", files, "/mock/rpm")
                self.assertEqual(query.call_count, 7)
            with mock.patch.object(rpm, "run", side_effect=subprocess.CalledProcessError(1, "rpm", stderr="bad digest")), \
                    self.assertRaises(subprocess.CalledProcessError):
                rpm.check_artifact(artifact, "1.0.0", files, "/mock/rpm")
            corruptions = (
                (rpm.METADATA_FORMAT, responses[rpm.METADATA_FORMAT].replace("noarch", "x86_64")),
                (rpm.METADATA_FORMAT, responses[rpm.METADATA_FORMAT].replace("gzip", "zstd")),
                (rpm.FILES_FORMAT, responses[rpm.FILES_FORMAT] + "/usr/share/sync-webdav-desktop/cache.pyc\t100644\troot\troot\t\tabc\n"),
                (rpm.FILES_FORMAT, responses[rpm.FILES_FORMAT].replace("100755", "100777", 1)),
                (rpm.FILES_FORMAT, responses[rpm.FILES_FORMAT].replace("\troot\t", "\tuser\t", 1)),
                (rpm.FILES_FORMAT, responses[rpm.FILES_FORMAT].replace("\t\t", "\t/tmp/secret\t", 1)),
                (rpm.FILES_FORMAT, responses[rpm.FILES_FORMAT].replace("\t\t", "\t\tbad", 1)),
                ("--requires", responses["--requires"] + "python(abi) = 3.14\n"),
                ("--requires", responses["--requires"] + "libc.so.6(GLIBC_2.34)(64bit)\n"),
                ("--requires", responses["--requires"].replace("python3 >= 3.8\n", "")),
                ("--recommends", "rclone\n"),
                ("--scripts", responses["--scripts"] + "systemctl --user start bad\n"),
                ("%|TRIGGERSCRIPTS?{[%{TRIGGERSCRIPTS}]}:{}|%|FILETRIGGERSCRIPTS?{[%{FILETRIGGERSCRIPTS}]}:{}|"
                 "%|TRANSFILETRIGGERSCRIPTS?{[%{TRANSFILETRIGGERSCRIPTS}]}:{}|", "unexpected trigger"),
            )
            for key, replacement in corruptions:
                with self.subTest(key=key, replacement=replacement[-70:]), \
                        mock.patch.object(rpm, "run", side_effect=self.fake_query(dict(responses, **{key: replacement}))), \
                        self.assertRaises(ValueError):
                    rpm.check_artifact(artifact, "1.0.0", files, "/mock/rpm")

    def test_mocked_build_is_unprivileged_and_output_paths_are_not_shell_text(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "space's %name;$(false)"
            def build(command, **kwargs):
                self.assertEqual(command[:3], ["/mock/rpmbuild", "--noplugins", "-bb"])
                self.assertNotIn(str(output), " ".join(command))
                topdir = Path(kwargs["cwd"])
                self.assertEqual(kwargs["env"]["HOME"], str(topdir / "HOME"))
                self.assertEqual(kwargs["env"]["SOURCE_DATE_EPOCH"], "42")
                self.assertEqual(topdir.parent, Path("/tmp"))
                self.assertTrue(Path(command[-1]).is_file())
                path = topdir / "RPMS/noarch" / rpm.artifact_name("1.0.0")
                path.parent.mkdir()
                path.write_bytes(b"mock built RPM")
                return ""
            with mock.patch.object(rpm, "prerequisite", side_effect=lambda name: "/mock/" + name), \
                    mock.patch.object(rpm, "run", side_effect=build) as command, \
                    mock.patch.object(rpm, "check_artifact") as check:
                path = rpm.build_rpm(output, "1.0.0", 42)
                self.assertEqual(path.read_bytes(), b"mock built RPM")
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                check.assert_called_once()
                command.assert_called_once()
                self.assertFalse(check.call_args[0][0].exists())

    def test_failed_validation_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with mock.patch.object(rpm, "prerequisite", return_value="/mock/tool"), \
                    mock.patch.object(rpm, "run", return_value=""), \
                    mock.patch.object(rpm, "check_artifact", side_effect=ValueError("invalid RPM")):
                with self.assertRaisesRegex(ValueError, "invalid RPM"):
                    rpm.build_rpm(output, "1.0.0", 42)
                self.assertFalse(output.exists())

    def test_cli_missing_prerequisites_no_output_and_no_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            for arguments, missing in (([], "rpmbuild"), (["--check-only"], "rpm")):
                error = io.StringIO()
                with mock.patch.object(rpm.shutil, "which", return_value=None), \
                        mock.patch.object(rpm.subprocess, "run") as forbidden, \
                        contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as result:
                    rpm.main(["--output-dir", str(output)] + arguments)
                self.assertEqual(result.exception.code, 1)
                self.assertIn("Missing prerequisite: " + missing, error.getvalue())
                self.assertFalse(output.exists())
                forbidden.assert_not_called()

    def test_cli_check_only_default_and_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            artifact = output / rpm.artifact_name("1.0.0")
            for arguments in (["--check-only", "--output-dir", str(output)],
                              ["--check-only", str(artifact)]):
                with mock.patch.object(rpm, "check_artifact") as check, \
                        mock.patch.object(rpm, "build_rpm") as forbidden, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(rpm.main(arguments), 0)
                    self.assertEqual(check.call_args[0][0], artifact)
                    forbidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()
