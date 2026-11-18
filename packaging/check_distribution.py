#!/usr/bin/python3
"""Offline packaging checks. All scratch files live under this checkout's dist/."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
# Read release metadata from source so future tags do not need a version bump
# in this checker. The helper validates literals without importing the GUI.
from release_notes import read_version

PACKAGE = "sync-webdav-desktop"
VERSION = read_version(ROOT / "install.py")
APP_ID = "io.github.vera.SyncWebDAVTray"
PREFIX = f"{PACKAGE}-{VERSION}"
ARTIFACTS = (f"{PREFIX}.tar.gz", f"{PACKAGE}_{VERSION}_all.deb")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run(command: list[str], cwd: Path = ROOT) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=120)
    if result.returncode:
        raise AssertionError(f"{shlex.join(command)} failed ({result.returncode}):\n"
                             f"{result.stdout}{result.stderr}")
    return result.stdout + result.stderr


def ar_members(path: Path) -> dict[str, bytes]:
    data = path.read_bytes()
    check(data[:8] == b"!<arch>\n", "Missing Debian ar signature")
    result = {}
    position = 8
    while position < len(data):
        header = data[position:position + 60]
        check(len(header) == 60 and header[-2:] == b"`\n", "Invalid ar member header")
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58])
        check(name not in result, "Duplicate ar member")
        check(int(header[28:34]) == 0 and int(header[34:40]) == 0, "Unnormalized ar owner")
        position += 60
        result[name] = data[position:position + size]
        check(len(result[name]) == size, "Truncated ar member")
        position += size + size % 2
    check(position == len(data), "Unexpected ar trailing data")
    return result


def tar_files(data: bytes) -> tuple[dict[str, bytes], list[tarfile.TarInfo]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        check(len({member.name for member in members}) == len(members), "Duplicate tar members")
        for member in members:
            check(not member.name.startswith("/") and ".." not in Path(member.name).parts,
                  f"Unsafe tar path: {member.name}")
            check(member.isfile() or member.isdir(), f"Non-regular tar member: {member.name}")
            check(member.uid == member.gid == 0 and member.uname == member.gname == "root",
                  f"Unnormalized tar owner: {member.name}")
            check(member.mode in (0o644, 0o755), f"Unexpected mode: {member.name}")
        check(len({member.mtime for member in members}) == 1, "Inconsistent tar timestamps")
        return {member.name: archive.extractfile(member).read()
                for member in members if member.isfile()}, members


def validate_desktop(path: Path) -> None:
    if shutil.which("desktop-file-validate"):
        run(["desktop-file-validate", str(path)])


def check_source_install(temp: Path) -> None:
    spec = importlib.util.spec_from_file_location("installer_under_test", ROOT / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    app_source = temp / "source copy"
    app_source.mkdir()
    for path in installer.runtime_files(ROOT):
        destination = app_source / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    # Check future application modules are included without a fixed-name list.
    for name in ("settings.py", "setup_engine.py", "authentication.py", "gui_setup.py", "wait_ready.py"):
        if not (app_source / name).exists():
            (app_source / name).write_text("# Packaging discovery fixture\n")
    (app_source / "obsolete_module.py").write_text("# Removed in the next version\n")
    home = temp / "home space % $ ` \" ' \\"
    data = home / "custom data % $ ` \" ' \\"
    config = home / "custom config"
    preserved = [home / ".sync-webdav/profiles/default.json",
                 config / "rclone/rclone.conf", config / "systemd/user/sync-webdav.service",
                 home / "sync-drive/cloud-file.txt"]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"UNCHANGED private data fixture\n")
    environment = {"XDG_DATA_HOME": str(data), "XDG_CONFIG_HOME": str(config)}
    application = data / PACKAGE
    desktop = data / "applications" / f"{APP_ID}.desktop"
    autostart = config / "autostart" / f"{APP_ID}.desktop"
    launcher = home / ".local/bin" / PACKAGE
    # No real HOME/xdg writes, cache refreshes, subprocesses, sudo or GUI launches.
    with mock.patch.object(installer, "SOURCE", app_source), \
            mock.patch.object(installer.Path, "home", return_value=home), \
            mock.patch.dict(os.environ, environment), \
            mock.patch.object(installer.os, "geteuid", return_value=1000), \
            mock.patch.object(installer, "refresh_desktop_cache") as refresh, \
            mock.patch.object(installer, "subprocess") as forbidden, \
            contextlib.redirect_stdout(io.StringIO()):
        check(installer.main([]) == 0, "Source install failed")
        check(autostart.exists(), "Default autostart missing")
        check((application / "wait_ready.py").read_bytes() == (app_source / "wait_ready.py").read_bytes(),
              "Readiness helper missing from stable copy")
        validate_desktop(desktop)
        validate_desktop(autostart)
        # Remove the source after install: generated launchers must stay stable.
        (app_source / "obsolete_module.py").unlink()
        (application / "user-note.txt").write_bytes(b"Preserve unowned file\n")
        check(installer.main(["--no-autostart"]) == 0, "Upgrade without autostart failed")
        check(not autostart.exists(), "--no-autostart failed to remove old entry")
        check(not (application / "obsolete_module.py").exists(), "Obsolete owned module was not removed")
        wrapper = launcher.read_text()
        check(shlex.split(wrapper.splitlines()[-1]) ==
              ["exec", "/usr/bin/python3", str(application / "app.py"), "$@"],
              "Shell launcher path quoting is unsafe")
        exec_line = next(line[5:] for line in desktop.read_text().splitlines() if line.startswith("Exec="))
        # First undo the Desktop Entry string-value escapes, then Exec quoting/field codes.
        value = exec_line.replace("\\\\", "\\")
        check(value.startswith('"') and value.endswith('"'), "Desktop Exec path is not quoted")
        argument = value[1:-1]
        decoded = []
        position = 0
        while position < len(argument):
            char = argument[position]
            if char == "\\":
                position += 1
                check(position < len(argument) and argument[position] in ('"', "$", "`", "\\"),
                      "Invalid Desktop Exec quote escape")
                char = argument[position]
            decoded.append(char)
            position += 1
        check("".join(decoded).replace("%%", "%") == str(launcher),
              "Desktop Exec path quoting is unsafe")
        check(str(app_source) not in wrapper, "Launcher still references the source directory")
        shutil.rmtree(app_source)
        check((application / "app.py").is_file(), "Installed app disappeared with source")
        check(installer.main(["--uninstall"]) == 0, "Uninstall failed without source files")
        check(not any(path.exists() for path in (launcher, desktop, autostart, application / "app.py")),
              "Uninstall left owned app files")
        check((application / "user-note.txt").read_bytes() == b"Preserve unowned file\n",
              "Uninstall removed unowned user data")
        for path in preserved:
            check(path.read_bytes() == b"UNCHANGED private data fixture\n",
                  f"Uninstall changed profiles/services/cloud files: {path}")
        forbidden.run.assert_not_called()
        check(refresh.call_count == 3, "Unexpected number of desktop cache refreshes")
    for value in ("bad\npath", "bad\rpath", "bad\x00path"):
        try:
            installer.desktop_quote(value)
        except ValueError:
            pass
        else:
            raise AssertionError("Desktop quoting accepts newline/NUL injection")
    for unsafe in ("../app.py", "icons/../../anything.svg", "/app.py", "install.py", "credentials.py"):
        check(not installer.is_runtime_path(Path(unsafe)), f"Unsafe runtime path accepted: {unsafe}")
    print("PASS source copy/upgrade/uninstall, future module discovery, and hostile-path quoting")
    if shutil.which("desktop-file-validate"):
        print("PASS desktop-file-validate (hostile-path per-user launcher and autostart)")


def main() -> int:
    (ROOT / "dist").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".packaging-check-", dir=ROOT / "dist") as directory:
        temp = Path(directory)
        first, second = temp / "first", temp / "second"
        for output in (first, second):
            run([sys.executable, "packaging/build.py", "--output-dir", str(output)])
        for name in ARTIFACTS:
            check((first / name).read_bytes() == (second / name).read_bytes(), f"Non-reproducible {name}")
            print(f"PASS reproducible {name}: {hashlib.sha256((first / name).read_bytes()).hexdigest()}")
        source, members = tar_files((first / ARTIFACTS[0]).read_bytes())
        names = {name[len(PREFIX) + 1:] for name in source}
        check(all(name.startswith(PREFIX + "/") for name in source), "Source archive is not portable/contained")
        check({"app.py", "backend.py", "recovery.py", "install.py", "scripts/bootstrap.py", "LICENSE",
               "docs/DISTRIBUTION.md", "packaging/build.py"} <= names, "Missing source release inputs")
        check(not any(part in {".git", "__pycache__", "dist", "runtime", "test-runtime", ".env"}
                      for name in names for part in Path(name).parts), "Excluded runtime/cache paths included")
        extracted = temp / "extracted"
        extracted.mkdir()
        # Only regular files/directories are allowed and all paths were checked above.
        for name, data in source.items():
            path = extracted / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        portable = extracted / PREFIX
        # Use an input-only checkout copy to prove excluded fixtures are not shipped.
        fixtures = [".env", "credentials.py", "secrets.json", "private.key", "rclone.conf",
                    "__pycache__/cached.pyc", "runtime/result.json", "tests/runtime/token.json",
                    "dist/stale.deb", ".git/config", ".venv/bin/python"]
        for name in fixtures:
            path = portable / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"DO NOT DISTRIBUTE\n")
        (portable / "app.py").touch()  # Different local mtimes must not change bytes.
        rebuilt = temp / "portable-build"
        run([sys.executable, "packaging/build.py", "--output-dir", str(rebuilt)], cwd=portable)
        for name in ARTIFACTS:
            check((first / name).read_bytes() == (rebuilt / name).read_bytes(),
                  f"Extracted source/fixture exclusions changed {name}")
        print(f"PASS portable source rebuild and secret/cache/runtime exclusions ({len(names)} included files)")
        ar = ar_members(first / ARTIFACTS[1])
        check(list(ar) == ["debian-binary", "control.tar.gz", "data.tar.gz"], "Unexpected Debian members/order")
        check(ar["debian-binary"] == b"2.0\n", "Incorrect Debian format marker")
        controls, _ = tar_files(ar["control.tar.gz"])
        payload, payload_members = tar_files(ar["data.tar.gz"])
        control = controls["control"].decode()
        for text in ("Package: sync-webdav-desktop\n", f"Version: {VERSION}\n", "Architecture: all\n",
                     "python3 (>= 3.8)", "gir1.2-ayatanaappindicator3-0.1", "util-linux, xdg-utils, pkexec | policykit-1\n"):
            check(text in control, f"Missing Debian metadata/dependency: {text}")
        runtime = {f"usr/share/{PACKAGE}/{name}" for name in names
                   if (len(Path(name).parts) == 1 and name.endswith(".py") and name != "install.py")
                   or name.startswith("icons/")}
        check(runtime <= payload.keys(), "Debian package omitted a runtime module or icon")
        check(f"usr/bin/{PACKAGE}" in payload, "Missing system wrapper")
        check(not any(name.endswith("install.py") or name.startswith("etc/") or name.startswith("home/")
                      for name in payload), "Debian package includes installer or user/service data")
        for name, data in payload.items():
            check(b"DO NOT DISTRIBUTE" not in data, f"Excluded fixture leaked: {name}")
        launcher = next(member for member in payload_members if member.name == f"usr/bin/{PACKAGE}")
        check(launcher.mode == 0o755, "System wrapper is not executable")
        desktop = temp / f"{APP_ID}.desktop"
        desktop.write_bytes(payload[f"usr/share/applications/{APP_ID}.desktop"])
        validate_desktop(desktop)
        if shutil.which("desktop-file-validate"):
            print("PASS desktop-file-validate (system entry)")
        else:
            print("SKIP desktop-file-validate (not installed)")
        if shutil.which("dpkg-deb"):
            print(run(["dpkg-deb", "--info", str(first / ARTIFACTS[1])]).rstrip())
            contents = run(["dpkg-deb", "--contents", str(first / ARTIFACTS[1])])
            check(f"./usr/share/{PACKAGE}/app.py" in contents or f"usr/share/{PACKAGE}/app.py" in contents,
                  "dpkg could not see application payload")
            print(f"PASS dpkg-deb --info/--contents ({len(payload)} payload files)")
        else:
            print("SKIP dpkg-deb inspection (not installed)")
        for name in ("postinst", "postrm"):
            path = temp / name
            path.write_bytes(controls[name])
            run(["/bin/sh", "-n", str(path)])
        run(["/bin/sh", "-n", str(ROOT / "packaging/sync-webdav-desktop")])
        check_source_install(temp)
        spec = importlib.util.spec_from_file_location("bootstrap_under_test", ROOT / "scripts/bootstrap.py")
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        with mock.patch.object(bootstrap, "missing_prerequisites", return_value=(True, ["pkexec"])), \
                mock.patch.object(bootstrap, "package_manager", return_value="apt-get"), \
                mock.patch.object(bootstrap, "authorization_package", return_value="pkexec"), \
                mock.patch.object(bootstrap.subprocess, "run") as forbidden, \
                contextlib.redirect_stdout(io.StringIO()):
            check(bootstrap.main(["--check"]) == 1, "Bootstrap check should report missing prerequisites")
            forbidden.assert_not_called()
        with mock.patch.object(bootstrap, "missing_prerequisites", return_value=(True, ["pkexec"])), \
                mock.patch.object(bootstrap, "package_manager", return_value="apt-get"), \
                mock.patch.object(bootstrap, "authorization_package", return_value="pkexec"), \
                mock.patch.object(bootstrap.os, "geteuid", return_value=1000), \
                mock.patch.object(bootstrap.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch.object(bootstrap.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(bootstrap.subprocess, "run") as forbidden, \
                contextlib.redirect_stdout(io.StringIO()):
            check(bootstrap.main([]) == 1, "Bootstrap must honor declined confirmation")
            forbidden.assert_not_called()
        with mock.patch.object(bootstrap, "missing_prerequisites", side_effect=[(True, ["pkexec"]), (False, [])]), \
                mock.patch.object(bootstrap, "package_manager", return_value="apt-get"), \
                mock.patch.object(bootstrap, "authorization_package", return_value="pkexec"), \
                mock.patch.object(bootstrap.os, "geteuid", return_value=1000), \
                mock.patch.object(bootstrap.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch.object(bootstrap.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="yes"), \
                mock.patch.object(bootstrap.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as install, \
                contextlib.redirect_stdout(io.StringIO()):
            check(bootstrap.main([]) == 0, "Confirmed bootstrap simulation failed")
            install.assert_called_once_with(["sudo", "apt-get", "install", "python3", "python3-gi",
                                             "gir1.2-gtk-3.0", "gir1.2-ayatanaappindicator3-0.1", "pkexec"],
                                            check=False)
        print("PASS shell syntax and bootstrap confirmation boundaries (all package calls mocked)")
    print("All packaging checks passed; no live installation or host user/service changes.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
