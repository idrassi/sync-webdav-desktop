"""Documented installation/configuration operations, independent of the GUI.

All privileged calls are fixed package/module/loginctl operations via pkexec.
Sync installation, profiles, configuration and services always run as the user.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass, replace
import http.client
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import json
import uuid
from typing import Callable

from diagnostics import (LOG_DROPIN, LOG_MARKER, legacy_logging_dropin, log_path,
                         logging_enabled, prepare_log)
from authentication import AuthenticationError, Cancelled, browser_login, terminate_group
from backend import (APP_DIR, OperationError, SERVICES, WEBDAV_BIN, change_services,
                     autostart_path, collect_snapshot, operation_lock, redact, run_command, set_autostart)
from settings import (HOME, Settings, atomic_write, config_dir, load_settings,
                      private_config_dir, reject_symlink_path, save_settings, settings_path, unit_dir)

INSTALLER_URL = "https://www10.sync.com/download/webdav/install.sh"
MARKER = "# Managed by Sync WebDAV Desktop"
Progress = Callable[[str], None]


@dataclass(frozen=True)
class Preflight:
    distro: str
    missing: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    fuse_ready: bool
    legacy: bool

    @property
    def ready(self) -> bool:
        return not self.errors


def distro_family(path: Path = Path("/etc/os-release")) -> str:
    values = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip('"')
    except OSError:
        return "unknown"
    identifiers = {values.get("ID", ""), *values.get("ID_LIKE", "").split()}
    if identifiers & {"ubuntu", "debian", "linuxmint", "zorin"}:
        return "debian"
    if identifiers & {'opensuse', 'opensuse-tumbleweed', 'opensuse-leap', 'suse', 'sles'}:
        return 'suse'
    if identifiers & {'rhel', 'centos', 'rocky', 'almalinux', 'ol'}:
        return 'rhel'
    if identifiers & {"fedora"}:
        return "fedora"
    if identifiers & {"arch", "manjaro"}:
        return "arch"
    return "unknown"


def package_commands(family: str) -> list[list[str]]:
    if family == "debian":
        return [["/usr/bin/apt-get", "update"], ["/usr/bin/apt-get", "install", "-y",
                "curl", "rclone", "fuse3", "util-linux", "kmod"]]
    if family in {'fedora', 'rhel'}:
        return [["/usr/bin/dnf", "install", "-y", "curl", "rclone", "fuse3", "util-linux", "kmod"]]
    if family == 'suse':
        return [['/usr/bin/zypper', '--non-interactive', 'install', '--no-recommends',
                 'curl', 'rclone', 'fuse3', 'util-linux', 'kmod']]
    if family == "arch":
        return [["/usr/bin/pacman", "-Syu", "--needed", "--noconfirm",
                 "curl", "rclone", "fuse3", "util-linux", "kmod"]]
    raise OperationError("Automatic dependency installation supports Ubuntu/Debian, Fedora/RHEL, openSUSE and Arch. Install prerequisites with your distribution's package tools.")


def preflight(settings: Settings) -> Preflight:
    settings = settings.validate()
    errors, warnings = [], []
    if os.geteuid() == 0:
        errors.append("Run this application as your normal user, never as root.")
    if os.uname().machine not in {"x86_64", "aarch64", "amd64", "arm64"}:
        errors.append("Sync's server supports only 64-bit Intel/AMD or ARM64 Linux.")
    if not Path("/run/systemd/system").exists():
        errors.append("Automatic mounts require a systemd Linux distribution.")
    missing = tuple(name for name in ("curl", "rclone", "fusermount3", "findmnt", "modprobe")
                    if not shutil.which(name) and not (name == "modprobe" and Path("/usr/sbin/modprobe").exists()))
    if not shutil.which("xdg-open"):
        errors.append("Browser sign-in requires xdg-open. Install the xdg-utils system package before connecting.")
    if not shutil.which("systemctl"):
        errors.append("systemctl is unavailable.")
    else:
        result = run_command(["systemctl", "--user", "show-environment"], timeout=5)
        if result.returncode:
            errors.append("Your systemd user session is unavailable. Launch from a logged-in Linux desktop.")
    if missing and distro_family() == "unknown":
        errors.append("Install missing requirements manually on this distribution: " + ", ".join(missing))
    if (missing or settings.before_login) and not shutil.which("pkexec"):
        errors.append("Administrator authorization requires pkexec (polkit). Install it with your package manager.")
    try:
        for managed_path in [config_dir(), settings.config_file, unit_dir()] + [unit_dir() / name for name in SERVICES]:
            reject_symlink_path(managed_path)
    except ValueError as exc:
        errors.append(str(exc))
    from mount_safety import inspect_mount
    mount_known = True
    try:
        mounted_entry = inspect_mount(settings)
    except OperationError as exc:
        errors.append(str(exc))
        mounted_entry = None
        mount_known = False
    path = Path(settings.mount_point)
    # Never stat/iterate the FUSE endpoint when mountinfo already shows it;
    # an orphaned endpoint can return ENOTCONN or hang while browsing.
    if not mount_known:
        pass  # Unknown mount state must not trigger a potentially blocking stat.
    elif mounted_entry is not None:
        warnings.append("The selected mount is already present; setup will stop services and verify cleanup before authentication.")
    elif path.exists() and not path.is_dir():
        errors.append("The selected mount location is not a directory.")
    elif path.exists() and not os.path.ismount(path):
        try:
            if any(path.iterdir()):
                errors.append("Mount folder is not empty. Choose an empty folder; existing local files will not be hidden or deleted.")
        except OSError:
            errors.append("Cannot access the selected mount folder.")
    parent = path.parent if mounted_entry is not None or not mount_known else path
    while not parent.exists():
        parent = parent.parent
    if not os.access(parent, os.W_OK):
        errors.append("The selected mount location is not writable.")
    fuse = os.access("/dev/fuse", os.R_OK | os.W_OK)
    if not fuse:
        warnings.append("FUSE is unavailable; setup will request approval to load the fuse kernel module.")
    legacy = any((unit_dir() / name).exists() for name in SERVICES) and not settings.managed
    if legacy:
        warnings.append("Existing Sync services will be adopted only after confirmation; configuration backups are kept privately.")
    if distro_family() == "arch" and missing:
        warnings.append("Arch dependency installation performs a full system upgrade, as required by the documented pacman -Syu instructions.")
    if distro_family() == 'rhel':
        warnings.append('RHEL/CentOS-compatible systems may require administrator-enabled EPEL and CRB/CodeReady Builder for Ayatana/Rclone. This app never adds repositories automatically.')
    if settings.before_login:
        warnings.append("Pre-login startup enables user lingering. It may keep other user services running after logout.")
    warnings.append("This is an independent app. Setup downloads and executes Sync's official HTTPS installer; Sync supplies the proprietary server.")
    return Preflight(distro_family(), missing, tuple(errors), tuple(warnings), fuse, legacy)


def run(args: list[str], cancel: threading.Event, timeout: int = 150,
        input_text: str | None = None, secret: bool = False,
        progress: Progress | None = None) -> str:
    """Cancellable bounded subprocess. Sensitive output is never used in errors."""
    if cancel.is_set():
        raise Cancelled("Operation cancelled")
    env = {key: val for key, val in os.environ.items()
           if key not in {"WEBDAV_USERNAME", "WEBDAV_PASSWORD", "SYNC_WEBDAV_DIR"}}
    env.update(LC_ALL="C", HOME=str(HOME))
    child = subprocess.Popen(args, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, env=env)
    deadline = time.monotonic() + timeout
    first = True
    try:
        while True:
            if cancel.is_set():
                raise Cancelled("Operation cancelled")
            if time.monotonic() > deadline:
                raise OperationError(f"{Path(args[0]).name} timed out. Check network access or administrator prompts.")
            try:
                stdout, stderr = child.communicate(input=input_text.encode() if first and input_text is not None else None, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                first = False
        if child.returncode:
            # Rclone/vendor commands may print paths, passwords or cloud names;
            # never embed their raw output in an application error.
            raise OperationError(f"{Path(args[0]).name} failed (exit {child.returncode}). Check the current-boot service diagnostics or retry the operation.")
        return stdout.decode("utf-8", "replace")
    finally:
        terminate_group(child)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream and not stream.closed:
                stream.close()


def privileged(args: list[str], cancel: threading.Event, progress: Progress) -> None:
    if not Path(args[0]).is_file():
        raise OperationError("Required administrator tool is missing: " + args[0])
    progress("Approve the administrator authorization dialog if you want to continue…")
    run(["/usr/bin/pkexec", *args], cancel, timeout=1200)


def install_requirements(settings: Settings, cancel: threading.Event, progress: Progress) -> None:
    report = preflight(settings)
    if report.errors:
        raise OperationError("\n".join(report.errors))
    if report.missing:
        for command in package_commands(report.distro):
            progress("Installing distribution requirements…")
            privileged(command, cancel, progress)
    if not os.access("/dev/fuse", os.R_OK | os.W_OK):
        modprobe = next((p for p in ["/usr/sbin/modprobe", "/sbin/modprobe", "/usr/bin/modprobe"] if Path(p).is_file()), None)
        if not modprobe:
            raise OperationError("modprobe is unavailable. Install kmod.")
        privileged([modprobe, "fuse"], cancel, progress)
    if not os.access("/dev/fuse", os.R_OK | os.W_OK):
        raise OperationError("FUSE is still inaccessible. Check distribution/device permissions; no automatic chmod is performed.")


def install_server(cancel: threading.Event, progress: Progress, update: bool = False) -> None:
    if WEBDAV_BIN.is_file() and os.access(WEBDAV_BIN, os.X_OK) and not update:
        progress("Using the installed Sync WebDAV server…")
        return
    progress("Downloading Sync's official installer over verified HTTPS…")
    with tempfile.TemporaryDirectory(prefix="installer-", dir=config_dir()) as directory:
        installer = Path(directory) / "install.sh"
        # Fixed URL, TLS verification, only HTTPS redirects. Never elevate it.
        run(["/usr/bin/curl", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
             "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "10",
             "--max-time", "60", "--output", str(installer), INSTALLER_URL], cancel, timeout=75)
        if installer.stat().st_size > 1024 * 1024 or not installer.read_bytes().startswith(b"#!/"):
            raise OperationError("Sync's installer response was not a valid shell script.")
        progress("Installing the vendor server; its installer verifies the binary checksum…")
        run(["/bin/sh", str(installer)], cancel, timeout=600)
    run([str(WEBDAV_BIN), "--version"], cancel, timeout=15)


def systemd_quote(value: str) -> str:
    # ExecStart has its own syntax, not a shell. %% is literal %, $$ is literal $.
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Control characters are not allowed in service arguments")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def service_files(settings: Settings) -> dict[str, str]:
    settings.validate()
    rclone = shutil.which("rclone") or "/usr/bin/rclone"
    python = "/usr/bin/python3"
    remote = settings.remote + ":/"
    config = "--config=" + str(settings.config_file)
    server_args = [str(WEBDAV_BIN), "--daemon", "--allow-from", "none", "--port", str(settings.port),
                   "--profile", settings.profile, "--log-file", str(log_path())]
    if settings.scope:
        server_args += ["--mount", settings.scope]
    def command(args): return " ".join(systemd_quote(arg) for arg in args)
    wait = command([python, str(APP_DIR / "wait_ready.py"), "--port", str(settings.port)])
    check = command([rclone, "lsd", remote, config, "--quiet", "--retries=1", "--low-level-retries=1", "--contimeout=10s", "--timeout=30s"])
    mount = command([rclone, "mount", remote, settings.mount_point, config,
                     "--dir-cache-time=1m", "--vfs-cache-mode=writes"])
    return {
        SERVICES[0]: f"""{MARKER}
[Unit]
Description=Sync WebDAV Server

[Service]
Type=forking
WorkingDirectory=%h
UMask=0077
ExecStartPre={command([python, str(APP_DIR / 'log_session.py')])}
ExecStart={command(server_args)}
Restart=on-failure
RestartSec=15
TimeoutStartSec=120

[Install]
WantedBy=default.target
""",
        SERVICES[1]: f"""{MARKER}
[Unit]
Description=Sync.com Rclone mount
Requires=sync-webdav.service
After=sync-webdav.service
PartOf=sync-webdav.service

[Service]
Type=notify
WorkingDirectory=%h
ExecStartPre={wait}
ExecStartPre={check}
ExecStart={mount}
Restart=on-failure
RestartSec=15
TimeoutStartSec=120

[Install]
WantedBy=default.target
"""}


def preserve_credential(settings: Settings, password: str) -> Path:
    """Durable issuance record before mounting; never discarded on rollback.

    This emergency record is independent of an installed/working rclone.
    Python has no AES in the standard library, so the private recovery record
    is plaintext, explicitly documented and 0600 within a 0700 directory.
    It is deleted on successful setup; never put it in logs or exports.
    """
    private_config_dir()
    directory = config_dir() / "credential-recovery"
    reject_symlink_path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / f"{settings.profile}-{uuid.uuid4()}.json"
    atomic_write(target, json.dumps({"profile": settings.profile, "port": settings.port,
                                   "username": "sync", "password": password,
                                   "created": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    return target


def recovered_credential(settings: Settings) -> tuple[str | None, Path | None]:
    directory = config_dir() / "credential-recovery"
    if not directory.exists():
        return None, None
    reject_symlink_path(directory)
    paths = sorted(directory.glob(settings.profile + "-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        reject_symlink_path(path)
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise OperationError("Unsafe credential recovery record permissions")
        value = json.loads(path.read_text())
        if value.get("profile") == settings.profile and value.get("username") == "sync" and value.get("password"):
            return value["password"], path
    return None, None


def clear_recovery(settings: Settings) -> None:
    directory = config_dir() / "credential-recovery"
    if directory.exists():
        reject_symlink_path(directory)
        for path in directory.glob(settings.profile + "-*.json"):
            reject_symlink_path(path)
            path.unlink()


def write_rclone(settings: Settings, password: str, cancel: threading.Event) -> None:
    if not password or "\n" in password or "\r" in password:
        raise OperationError("The generated WebDAV credential could not be parsed. Retry setup.")
    # Unlike the documented config-create argv example, stdin keeps the
    # plaintext password out of process lists, histories, and diagnostic logs.
    obscured = run([shutil.which("rclone") or "/usr/bin/rclone", "obscure", "-"],
                   cancel, input_text=password + "\n", secret=True).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", obscured):
        raise OperationError("Rclone returned an invalid obscured credential")
    parser = configparser.ConfigParser(interpolation=None)
    path = settings.config_file
    try:
        if path.exists():
            parser.read_string(path.read_text())
    except (OSError, configparser.Error):
        raise OperationError("Existing Rclone configuration cannot be safely read; no changes were written") from None
    parser[settings.remote] = {"type": "webdav", "url": f"http://127.0.0.1:{settings.port}",
                               "vendor": "other", "user": "sync", "pass": obscured}
    import io
    stream = io.StringIO()
    parser.write(stream)
    atomic_write(path, stream.getvalue())


def rclone_test(settings: Settings, cancel: threading.Event) -> None:
    if not settings.config_file.is_file():
        raise OperationError("No Rclone credential is configured. Use Connect with 'Replace local WebDAV credential' approved to rebuild it.")
    run([shutil.which("rclone") or "/usr/bin/rclone", "lsd", settings.remote + ":/",
         "--config=" + str(settings.config_file), "--quiet", "--retries=1", "--low-level-retries=1",
         "--contimeout=10s", "--timeout=30s"], cancel, timeout=45, secret=True)


def wait_server(settings: Settings, cancel: threading.Event) -> None:
    for _ in range(45):
        if cancel.wait(0.5):
            raise Cancelled("Operation cancelled")
        connection = http.client.HTTPConnection("127.0.0.1", settings.port, timeout=1)
        try:
            connection.request("HEAD", "/")
            if connection.getresponse().status in {200, 204, 207, 401}:
                return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
    raise OperationError("The local Sync WebDAV server did not become ready. Check authentication and service diagnostics.")


class Transaction:
    """Private file backups and rollback; never remove mount/cloud contents."""
    def __init__(self, settings: Settings):
        self.files = {}
        self.issued_password = None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        root = config_dir() / "backups"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=root))
        bookmark_path = Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "gtk-3.0/bookmarks"
        logging_override = unit_dir() / 'sync-webdav.service.d' / LOG_DROPIN
        self.mount_settings = settings
        self.previous_settings = load_settings()
        for path in [unit_dir() / name for name in SERVICES] + [settings.config_file, settings_path(), autostart_path(), bookmark_path, logging_override]:
            if path.is_symlink():
                raise OperationError(f"Refusing to replace symbolic link: {path}")
            self.files[path] = path.read_bytes() if path.is_file() else None
            if self.files[path] is not None:
                atomic_write(self.directory / f"{len(self.files)}-{path.name}", self.files[path])
        self.linger_changed = False
        user = pwd.getpwuid(os.getuid()).pw_name
        linger = run_command(["loginctl", "show-user", user, "--property=Linger", "--value"])
        self.had_linger = linger.returncode == 0 and linger.stdout.strip() == "yes"
        self.states = {}
        for name in SERVICES:
            result = run_command(["systemctl", "--user", "show", name, "--property=ActiveState,UnitFileState"])
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            self.states[name] = values

    def credential_issued(self, password: str) -> None:
        self.issued_password = password

    def rollback(self, progress: Progress) -> None:
        progress("Restoring the previous local configuration after unsuccessful setup…")
        if self.linger_changed and not self.had_linger:
            user = pwd.getpwuid(os.getuid()).pw_name
            result = run_command(["/usr/bin/pkexec", "/usr/bin/loginctl", "disable-linger", user], timeout=120)
            if result.returncode:
                progress("Pre-login startup could not be rolled back; user lingering remains enabled. Other user services may keep running after logout.")
        for name in SERVICES[::-1]:
            result = run_command(["systemctl", "--user", "stop", name], timeout=150)
            if result.returncode:
                state = run_command(["systemctl", "--user", "show", name, "--property=LoadState"])
                if "LoadState=not-found" not in state.stdout:
                    raise OperationError("Rollback could not stop a Sync service. Private backups are kept at " + str(self.directory))
        from mount_safety import cleanup_stale_mount
        cleanup_stale_mount(self.mount_settings, progress=progress)
        if self.previous_settings.configured and self.previous_settings.mount_point != self.mount_settings.mount_point:
            cleanup_stale_mount(self.previous_settings, progress=progress)
        for path, value in self.files.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, value, 0o600)
        restored = None
        for path, value in self.files.items():
            if path == settings_path() and value is not None:
                restored = Settings(**json.loads(value.decode())).validate()
        if restored is None:
            restored = load_settings()
        if self.issued_password and restored.configured:
            # Same-profile restriction above makes identity consistent; use
            # the RESTORED endpoint/options with the newly issued password.
            write_rclone(restored, self.issued_password, threading.Event())
        self.issued_password = None
        result = run_command(["systemctl", "--user", "daemon-reload"])
        if result.returncode:
            raise OperationError("Rollback could not reload user services")
        for name, state in self.states.items():
            enabled = state.get("UnitFileState")
            if enabled not in {"enabled", "enabled-runtime", "disabled", "not-found", ""}:
                continue  # Do not rewrite masks, static units or external policy.
            action = "enable" if enabled in {"enabled", "enabled-runtime"} else "disable"
            command = ["systemctl", "--user", action]
            if enabled == "enabled-runtime": command.append("--runtime")
            command.append(name)
            result = run_command(command)
            if result.returncode and enabled not in {"not-found", ""}:
                raise OperationError("Rollback could not restore service startup state")
        for name, state in self.states.items():
            if state.get("ActiveState") == "active":
                result = run_command(["systemctl", "--user", "start", name], timeout=150)
                if result.returncode:
                    raise OperationError("Rollback restored files but could not restart previous services; use Sign in & restore")


def ensure_port_free(settings: Settings) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", settings.port))
        except OSError as exc:
            raise OperationError(f"Port {settings.port} is in use by another process. Stop your manually started WebDAV server or choose another port; this app does not kill unrelated processes.") from exc


def add_bookmark(settings: Settings) -> None:
    path = Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "gtk-3.0/bookmarks"
    uri = Path(settings.mount_point).as_uri()
    old = path.read_text() if path.exists() else ""
    if not any(line.split(" ", 1)[0] == uri for line in old.splitlines()):
        atomic_write(path, old.rstrip("\n") + ("\n" if old else "") + uri + " Sync Drive\n")


def connect(settings: Settings, cancel: threading.Event, progress: Progress,
            reset_credential: bool = False, approve_adoption: bool = False,
            allow_lazy: bool = False) -> Settings:
    """Onboard or reconfigure one account/mount. Never use sudo for user files."""
    settings = settings.validate()
    report = preflight(settings)
    if report.errors:
        raise OperationError("\n".join(report.errors))
    # This application intentionally manages one active mount. Switching the
    # credential-bearing account/profile in place would make rollback unable
    # to restore the former cloud identity. Remove setup before switching.
    previous = load_settings()
    if previous.configured and settings.profile != previous.profile:
        raise OperationError("To switch Sync profiles, first remove the current automatic mount setup. Existing credentials and cloud files are kept.")
    if report.legacy and not approve_adoption:
        raise OperationError("Existing Sync services require explicit adoption approval")
    with operation_lock():
        private_config_dir()
        install_requirements(settings, cancel, progress)
        install_server(cancel, progress)
        transaction = Transaction(settings)
        password = None
        try:
            progress("Stopping existing Sync services before authentication…")
            for name in SERVICES[::-1]:
                # Missing units are expected on a first installation.
                result = run_command(["systemctl", "--user", "show", name, "--property=LoadState"])
                if "LoadState=loaded" in result.stdout:
                    run(["systemctl", "--user", "stop", name], cancel, timeout=150)
            # Both the old and proposed paths must be clear before changing
            # configuration. Never list a dead FUSE endpoint.
            from mount_safety import cleanup_stale_mount
            if previous.configured:
                cleanup_stale_mount(previous, allow_lazy=allow_lazy, progress=progress)
            if settings.mount_point != previous.mount_point or not previous.configured:
                cleanup_stale_mount(settings, allow_lazy=allow_lazy, progress=progress)
            ensure_port_free(settings)
            issuance = []
            def issued(value):
                issuance.append(preserve_credential(settings, value))
                transaction.credential_issued(value)
            password = browser_login(WEBDAV_BIN, settings.profile, cancel, progress,
                                     reset=reset_credential, on_credential=issued)
            if password and not issuance:
                issued(password)
            if not password:
                password, _ = recovered_credential(settings)
            if password:
                transaction.credential_issued(password)
                write_rclone(settings, password, threading.Event())
            elif not settings.config_file.is_file():
                raise OperationError("This Sync profile already has a local WebDAV password that cannot be recovered. Retry Connect and approve 'Replace local WebDAV credential'; other WebDAV clients must then be updated.")
            password = None
            progress("Creating the mount folder and automatic user services…")
            Path(settings.mount_point).mkdir(mode=0o700, parents=True, exist_ok=True)
            # Recheck after login in case the user put local files here meanwhile.
            if not os.path.ismount(settings.mount_point) and any(Path(settings.mount_point).iterdir()):
                raise OperationError("The mount folder now contains local files. Choose an empty folder.")
            prepare_log()
            for name, content in service_files(settings).items():
                atomic_write(unit_dir() / name, content)
            override = unit_dir() / 'sync-webdav.service.d' / LOG_DROPIN
            if override.exists():
                if not override.read_text().startswith(LOG_MARKER):
                    raise OperationError("The logging override was modified externally; it was not removed")
                override.unlink()
            run(["systemctl", "--user", "daemon-reload"], cancel)
            action = "enable" if settings.start_services else "disable"
            run(["systemctl", "--user", action, *SERVICES], cancel)
            if settings.before_login:
                user = pwd.getpwuid(os.getuid()).pw_name
                transaction.linger_changed = True
                privileged(["/usr/bin/loginctl", "enable-linger", user], cancel, progress)
            progress("Starting Sync WebDAV and testing the authenticated Rclone connection…")
            run(["systemctl", "--user", "start", SERVICES[0]], cancel, timeout=150)
            wait_server(settings, cancel)
            rclone_test(settings, cancel)
            progress("Mounting your Sync files…")
            run(["systemctl", "--user", "start", SERVICES[1]], cancel, timeout=150)
            verified = replace(settings, managed=True, configured=True)
            snapshot = collect_snapshot(verified)
            if snapshot.state != "online":
                raise OperationError("Services started but the expected mount could not be verified")
            save_settings(verified)
            set_autostart(settings.tray_autostart)
            if settings.bookmark:
                add_bookmark(settings)
            clear_recovery(settings)
            transaction.issued_password = None
            progress("Connected. The drive is mounted and configuration is saved.")
            return verified
        except BaseException as original:
            password = None
            try:
                transaction.rollback(progress)
            except Exception as rollback_error:
                from mount_safety import StaleMountBusy
                if isinstance(original, StaleMountBusy) and isinstance(rollback_error, StaleMountBusy):
                    raise original
                raise OperationError("Setup failed and rollback needs attention: " + str(rollback_error)) from original
            raise


def enable_failure_logging(settings: Settings, cancel: threading.Event, progress: Progress) -> None:
    """Only after user action: add private logging without signing in or starting services."""
    if logging_enabled():
        prepare_log()
        return
    source = unit_dir() / SERVICES[0]
    if not source.is_file():
        raise OperationError("Install/configure Sync before enabling daemon logging")
    override = unit_dir() / 'sync-webdav.service.d' / LOG_DROPIN
    reject_symlink_path(override)
    if override.exists() and not override.read_text().startswith(LOG_MARKER):
        raise OperationError("An externally modified logging override will not be replaced")
    # Managed units already include logging; this override preserves simple
    # legacy guide commands and never claims ownership of the whole unit.
    content = legacy_logging_dropin(WEBDAV_BIN)
    prepare_log()
    previous = override.read_bytes() if override.exists() else None
    atomic_write(override, content)
    try:
        run(["systemctl", "--user", "daemon-reload"], cancel)
    except BaseException:
        if previous is None:
            override.unlink(missing_ok=True)
        else:
            atomic_write(override, previous)
        run_command(["systemctl", "--user", "daemon-reload"])
        raise
    progress("Private daemon failure logging enabled; it takes effect on the next server start.")


def configure_failure_logging(settings: Settings, cancel: threading.Event, progress: Progress) -> None:
    with operation_lock():
        enable_failure_logging(settings, cancel, progress)


def stop_and_cleanup(settings: Settings, cancel: threading.Event, progress: Progress,
                     allow_lazy: bool = False) -> None:
    if cancel.is_set():
        raise Cancelled("Operation cancelled")
    change_services("stop")
    if cancel.is_set():
        raise Cancelled("Operation cancelled; services are stopped")
    from mount_safety import cleanup_stale_mount
    cleanup_stale_mount(settings, allow_lazy=allow_lazy, progress=progress)


def restart(settings: Settings, cancel: threading.Event, progress: Progress,
            allow_lazy: bool = False):
    """Explicit stop-cleanup-start, not restart into an orphaned FUSE mount."""
    with operation_lock():
        progress("Stopping services and checking for a stale Sync mount…")
        stop_and_cleanup(settings, cancel, progress, allow_lazy)
        enable_failure_logging(settings, cancel, progress)
        progress("Starting Sync WebDAV…")
        run(["systemctl", "--user", "start", SERVICES[0]], cancel, timeout=150)
        wait_server(settings, cancel)
        rclone_test(settings, cancel)
        run(["systemctl", "--user", "start", SERVICES[1]], cancel, timeout=150)
        snapshot = collect_snapshot(settings)
        if snapshot.state != 'online':
            raise OperationError(snapshot.title + '. Check service diagnostics.')
        return snapshot


def restore(settings: Settings, cancel: threading.Event, progress: Progress,
            allow_lazy: bool = False) -> None:
    with operation_lock():
        progress("Stopping Sync services and checking for an orphaned mount…")
        stop_and_cleanup(settings, cancel, progress, allow_lazy)
        enable_failure_logging(settings, cancel, progress)
        issuance = []
        def issued(value):
            issuance.append(preserve_credential(settings, value))
        password = browser_login(WEBDAV_BIN, settings.profile, cancel, progress, on_credential=issued)
        if password and not issuance:
            issued(password)
        if not password:
            password, _ = recovered_credential(settings)
        if password:
            write_rclone(settings, password, threading.Event())
        password = None
        progress("Starting WebDAV and verifying the connection…")
        run(["systemctl", "--user", "start", SERVICES[0]], cancel, timeout=150)
        wait_server(settings, cancel)
        rclone_test(settings, cancel)
        run(["systemctl", "--user", "start", SERVICES[1]], cancel, timeout=150)
        if collect_snapshot(settings).state != "online":
            raise OperationError("The mount is not ready. Check service diagnostics.")
        clear_recovery(settings)


def update_server(settings: Settings, cancel: threading.Event, progress: Progress,
                  allow_lazy: bool = False) -> None:
    with operation_lock():
        snapshot = collect_snapshot(settings)
        stop_and_cleanup(settings, cancel, progress, allow_lazy)
        enable_failure_logging(settings, cancel, progress)
        try:
            private_config_dir()
            install_server(cancel, progress, update=True)
        finally:
            if snapshot.webdav.running:
                # Once installation stops, restore the prior service state even
                # after cancellation. The installer never deletes cloud data.
                units = SERVICES if snapshot.rclone.running else (SERVICES[0],)
                result = run_command(["systemctl", "--user", "start", *units], timeout=150)
                if result.returncode:
                    raise OperationError("The server update finished but the previous service state could not be restored")


def disconnect(settings: Settings, remove_services: bool = False, allow_lazy: bool = False) -> None:
    with operation_lock():
        if remove_services:
            if not settings.managed:
                raise OperationError("Only services explicitly managed by this application can be removed")
            for name in SERVICES:
                path = unit_dir() / name
                if path.exists() and not path.read_text().startswith(MARKER):
                    raise OperationError("A service was modified externally; it will not be removed automatically")
        stop_and_cleanup(settings, threading.Event(), lambda _: None, allow_lazy)
        if remove_services:
            result = run_command(["systemctl", "--user", "disable", *SERVICES])
            if result.returncode:
                raise OperationError("Could not disable startup services; no service files were removed")
            for name in SERVICES:
                (unit_dir() / name).unlink(missing_ok=True)
            override = unit_dir() / 'sync-webdav.service.d' / LOG_DROPIN
            if override.exists() and override.read_text().startswith(LOG_MARKER):
                override.unlink()
            run_command(["systemctl", "--user", "daemon-reload"])
            save_settings(replace(settings, configured=False, managed=False))
