"""Read-only monitoring and explicitly requested recovery for Sync's user services."""
from __future__ import annotations

import contextlib
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from dataclasses import asdict, dataclass

from desktop_entry import desktop_quote

APP_ID = "io.github.vera.SyncWebDAVTray"
SERVICES = ("sync-webdav.service", "sync-rclone.service")
HOME = Path.home()
MOUNT_POINT = HOME / "sync-drive"
WEBDAV_BIN = HOME / ".sync-webdav/bin/sync-webdav"
APP_DIR = Path(__file__).resolve().parent
SYSTEMCTL = "/usr/bin/systemctl"


class OperationError(RuntimeError):
    pass


def redact(text: str) -> str:
    """Best-effort masking for diagnostics; never read profile/config credentials."""
    text = re.sub(r"(?im)^.*(?:authorization|set-cookie|cookie)\s*[:=].*$",
                  "[REDACTED authentication header]", text)
    text = re.sub(r"(?i)(\b(?:password|passwd|pass|access_token|refresh_token|token|secret|"
                  r"auth_code|code_verifier)\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(\w+://)[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(https?://[^\s?#]+)[?#][^\s]+", r"\1?[REDACTED]", text)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL REDACTED]", text)


def run_command(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as exc:
        raise OperationError(f"{Path(args[0]).name} timed out after {timeout} seconds.") from exc
    except OSError as exc:
        raise OperationError(f"Cannot run {Path(args[0]).name}: {exc.strerror}") from exc


def change_services(action: str) -> None:
    if action not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported service action")
    # Stop the dependent mount first; start/restart both explicitly because a
    # previously failed dependency job is not necessarily retried by systemd.
    units = SERVICES[::-1] if action == "stop" else SERVICES
    result = run_command([SYSTEMCTL, "--user", action, *units], timeout=150)
    if result.returncode:
        detail = redact(result.stderr or result.stdout).strip()[-2000:]
        raise OperationError(detail or f"Could not {action} Sync services.")


@dataclass(frozen=True)
class Unit:
    name: str
    active: str = "unknown"
    sub: str = "unknown"
    enabled: str = "unknown"
    result: str = "unknown"
    invocation: str = ""
    loaded: str = "unknown"
    main_pid: int = 0
    active_exit_monotonic: int = 0

    @property
    def running(self) -> bool:
        return self.active == "active"

    @property
    def description(self) -> str:
        return f"{self.active} / {self.sub} · startup: {self.enabled}"


def parse_units(output: str) -> dict[str, Unit]:
    units = {}
    for block in output.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = fields.get("Id")
        if name in SERVICES:
            units[name] = Unit(name, fields.get("ActiveState", "unknown"),
                               fields.get("SubState", "unknown"),
                               fields.get("UnitFileState", "unknown"),
                               fields.get("Result", "unknown"), fields.get("InvocationID", ""),
                               fields.get("LoadState", "unknown"),
                               int(fields.get("MainPID", "0") or "0"),
                               int(fields.get("ActiveExitTimestampMonotonic", "0") or "0"))
    return {name: units.get(name, Unit(name)) for name in SERVICES}


def parse_journal(output: str) -> list[dict]:
    entries = []
    for line in output.splitlines():
        try:
            entry = json.loads(line)
            message = entry.get("MESSAGE", "")
            if not isinstance(message, str):
                continue
            entries.append(entry)
        except (ValueError, AttributeError):
            continue
    return entries


def auth_rejected(entries: list[dict], webdav: Unit) -> bool:
    from diagnostics import auth_observation
    return not webdav.running and bool(auth_observation(entries))


def format_journal(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        try:
            timestamp = time.strftime("%H:%M:%S", time.localtime(
                int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000))
        except (ValueError, TypeError, OverflowError):
            timestamp = "--:--:--"
        lines.append(f"{timestamp}  {redact(entry.get('MESSAGE', ''))}")
    return "\n".join(lines)[-16000:] or "No Sync service logs from this boot."


@dataclass(frozen=True)
class Snapshot:
    webdav: Unit
    rclone: Unit
    mounted: bool
    mount_type: str
    http_status: int | None
    auth_failure: bool
    state: str
    title: str
    detail: str
    logs: str
    checked_at: str
    recovery_busy: bool = False
    mount_point: str = str(MOUNT_POINT)
    port: int = 4918
    configured: bool = False
    managed: bool = False
    auth_reason: str = ""
    latest_auth_reason: str = ""
    stale_mount: bool = False
    failure_logging: bool = False
    session_issued_at: str = ''
    session_age_seconds: float | None = None
    last_keepalive_success_at: str = ''
    keepalive_success_count: int = 0
    keepalive_failure_count: int = 0
    session_evidence: str = ''
    keepalive_history: str = ''
    http_timed_out: bool = False

    @property
    def webdav_delayed(self) -> bool:
        return (self.http_timed_out and self.mounted and self.webdav.running
                and self.rclone.running and not self.auth_failure)

    def as_dict(self) -> dict:
        return asdict(self)


def classify(webdav: Unit, rclone: Unit, mounted: bool, http_status: int | None,
             auth_failure: bool, mount_point: str | None = None,
             auth_reason: str = "", stale_mount: bool = False) -> tuple[str, str, str]:
    if webdav.running and rclone.running and mounted and http_status in {200, 204, 207, 401}:
        return "online", "Sync drive is connected", f"Your files are mounted at {mount_point or MOUNT_POINT}."
    if auth_failure:
        from session_guidance import AUTO_LOGOUT_HINT
        titles = {"expired": "Sync session expired — sign-in required",
                  "revoked": "Sync session rejected — sign-in required",
                  "missing": "Sync sign-in is missing", "invalid": "Sync sign-in is invalid",
                  "unknown": "Sync session validation failed"}
        detail = ("Sync reported an expired saved session. " + AUTO_LOGOUT_HINT + " Use Sign in & restore after reviewing the policy."
                  if auth_reason == "expired" else
                  "The SDK session check failed without a clear cause. A vendor 'revoked' label after this failure is not proof of account revocation. Check diagnostics before signing in again."
                  if auth_reason == "unknown" else
                  "Sync rejected the saved session. Use Sign in & restore to authenticate in your browser.")
        if stale_mount:
            detail += " A stale mount also remains. Close Files windows and open files on the drive before recovery."
        return "auth", titles.get(auth_reason, "Sync sign-in required"), detail
    if stale_mount:
        return "offline", "Sync drive has a stale mount", (
            "Rclone stopped but its FUSE mount remains. Close Files windows and open files on the drive, then use Restart services. Busy mounts require explicit cleanup approval.")
    if any(unit.loaded == "not-found" for unit in (webdav, rclone)):
        return "offline", "Sync setup is missing", "One or both Sync user services are not installed."
    if any(unit.active == "activating" for unit in (webdav, rclone)):
        return "working", "Sync is connecting", "Waiting for the WebDAV server and Rclone mount."
    if webdav.active == "inactive" and rclone.active == "inactive":
        return "offline", "Sync services are stopped", "Choose Restart services to reconnect."
    return "offline", "Sync drive needs attention", (
        "The mount is not ready. Try Restart services; if the logs show authentication "
        "rejection, use Sign in & restore. Do not save files into an unmounted drive folder.")


def private_runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else HOME / ".cache"
    path = base / "sync-webdav-tray"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid() or not path.is_dir():
        raise OperationError("Unsafe Sync tray runtime directory")
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def operation_lock(shared: bool = False):
    path = private_runtime_dir() / "recovery.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationError("Another Sync recovery operation is already running.") from exc
        yield
    finally:
        os.close(fd)


def operation_busy() -> bool:
    try:
        # Read-only keep-alives hold a shared lock. They do not count as
        # recovery and must not turn a healthy drive's tray icon amber.
        with operation_lock(shared=True):
            return False
    except OperationError:
        return True


def collect_snapshot(settings=None) -> Snapshot:
    from settings import load_settings
    from diagnostics import (auth_observation, daemon_auth_cause, daemon_log_tail, failure_window,
                             logging_enabled, original_auth_cause)
    settings = settings or load_settings()
    errors = []
    try:
        result = run_command([SYSTEMCTL, "--user", "show", *SERVICES, "--no-pager",
                              "--property=Id,LoadState,ActiveState,SubState,UnitFileState,Result,InvocationID,MainPID,ActiveExitTimestampMonotonic"])
        units = parse_units(result.stdout)
        if result.returncode:
            errors.append(redact(result.stderr).strip())
    except OperationError as exc:
        units = parse_units("")
        errors.append(str(exc))
    try:
        result = run_command(["/usr/bin/findmnt", "--mountpoint", settings.mount_point,
                              "--noheadings", "--output", "FSTYPE,SOURCE"], timeout=5)
        fields = result.stdout.strip().split(maxsplit=1)
        # An unrelated filesystem at this path does not count as a Sync mount.
        mounted = (result.returncode == 0 and len(fields) == 2
                   and fields[0] == "fuse.rclone" and fields[1].rstrip("/") == settings.remote + ":")
        mount_type = result.stdout.strip() or "Not mounted"
    except OperationError as exc:
        mounted, mount_type = False, "Unknown"
        errors.append(str(exc))
    http_status = None
    http_timed_out = False
    connection = http.client.HTTPConnection("127.0.0.1", settings.port, timeout=2)
    try:
        connection.request("HEAD", "/")
        http_status = connection.getresponse().status
    except (TimeoutError, socket.timeout):  # Distinct exception classes before Python 3.10.
        http_timed_out = True
    except (OSError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    try:
        result = run_command(["/usr/bin/journalctl", "--user", "-b", "-u", SERVICES[0],
                              "-u", SERVICES[1], "-n", "70", "--no-pager", "-o", "json",
                              "--output-fields=MESSAGE,_SYSTEMD_USER_UNIT,_SYSTEMD_INVOCATION_ID,__REALTIME_TIMESTAMP"])
        entries = parse_journal(result.stdout)
        if result.returncode:
            errors.append(redact(result.stderr).strip())
    except OperationError as exc:
        entries = []
        errors.append(str(exc))
    webdav, rclone = (units[name] for name in SERVICES)
    latest_reason = auth_observation(entries) if not webdav.running else ""
    first_reason = ""
    failure_entries = []
    window = failure_window(webdav.active_exit_monotonic) if not webdav.running else None
    if window:
        try:
            result = run_command(["/usr/bin/journalctl", "--user", "-b", "-u", SERVICES[0],
                                  "--since", window[0], "--until", window[1], "-n", "150", "--no-pager", "-o", "json",
                                  "--output-fields=MESSAGE,_SYSTEMD_USER_UNIT,_SYSTEMD_INVOCATION_ID,__REALTIME_TIMESTAMP"])
            if result.returncode == 0:
                failure_entries = parse_journal(result.stdout)
                first_reason = original_auth_cause(failure_entries)
        except OperationError as exc:
            errors.append(str(exc))
    daemon_text = ""
    try:
        daemon_text = daemon_log_tail()
        captured_reason = daemon_auth_cause(daemon_text, webdav.active_exit_monotonic) if not webdav.running else ""
        first_reason = captured_reason or first_reason
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    reason = first_reason or latest_reason
    auth = bool(latest_reason or first_reason)
    stale = mounted and not rclone.running and rclone.main_pid == 0 and rclone.active != "activating"
    state, title, detail = classify(webdav, rclone, mounted, http_status, auth, settings.mount_point, reason, stale)
    if not settings.configured and webdav.loaded == "not-found":
        state, title, detail = "setup", "Welcome to Sync WebDAV Desktop", "Choose Connect to Sync to install the server, authenticate in your browser, and mount your files."
    logs = format_journal(entries)
    if first_reason:
        logs = "Original failure: " + first_reason + "\n" + format_journal(failure_entries) + "\n\nRecent retries:\n" + logs
    logs += "\n\nPrivate WebDAV daemon log (credential masking applied):\n" + daemon_text
    if errors:
        logs += "\n\nMonitor diagnostics:\n" + "\n".join(errors)
    snapshot = Snapshot(webdav, rclone, mounted, mount_type, http_status, auth,
                    state, title, detail, logs, time.strftime("%H:%M:%S"), operation_busy(),
                    settings.mount_point, settings.port, settings.configured, settings.managed,
                    reason, latest_reason, stale, logging_enabled(), http_timed_out=http_timed_out)
    from activity_history import report
    from dataclasses import replace
    try:
        evidence = report(settings, snapshot)
        return replace(snapshot, session_issued_at=evidence['session_issued_at'],
                       session_age_seconds=evidence['session_age_seconds'],
                       last_keepalive_success_at=evidence['last_success_at'],
                       keepalive_success_count=evidence['successful_reads'],
                       keepalive_failure_count=evidence['failed_reads'],
                       session_evidence=evidence['summary'], keepalive_history=evidence['history'])
    except (OSError, ValueError):
        return replace(snapshot, session_evidence='Session / keep-alive evidence unavailable; check private history permissions.')


def restart_services(settings=None, allow_lazy: bool = False) -> Snapshot:
    from settings import load_settings
    from setup_engine import restart
    import threading
    settings = settings or load_settings()
    return restart(settings, threading.Event(), lambda _: None, allow_lazy)


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config")))


def desktop_entry(tray: bool = False) -> str:
    program = desktop_quote(str(APP_DIR / "app.py"))
    return ("[Desktop Entry]\nType=Application\nName=Sync WebDAV Desktop\n"
            "Comment=Set up, configure, and restore your Sync.com WebDAV drive\n"
            f"Exec=/usr/bin/python3 {program}" + (" --tray" if tray else "") + "\n"
            f"Icon={APP_ID}\nTerminal=false\nCategories=Network;\n"
            "Keywords=Sync;WebDAV;rclone;mount;cloud;\nStartupNotify=false\n"
            + ("X-GNOME-Autostart-Delay=15\n" if tray else ""))


def autostart_path() -> Path:
    return config_home() / "autostart" / f"{APP_ID}.desktop"


def set_autostart(enabled: bool) -> None:
    from settings import atomic_write, reject_symlink_path
    path = autostart_path()
    if enabled:
        atomic_write(path, desktop_entry(tray=True))
    else:
        reject_symlink_path(path)
        path.unlink(missing_ok=True)
