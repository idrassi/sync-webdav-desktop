"""Private vendor logging and authentication evidence, without credential access."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import stat
import time

from settings import config_dir, private_config_dir, reject_symlink_path, unit_dir

LOG_DROPIN = "90-sync-desktop-logging.conf"
LOG_MARKER = "# Sync WebDAV Desktop private daemon logging"
MAX_TAIL = 64 * 1024


def log_path() -> Path:
    return config_dir() / "logs" / "webdav.log"


def prepare_log() -> Path:
    private_config_dir()
    path = log_path()
    reject_symlink_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid():
        raise ValueError("Daemon log directory is not owned by this user")
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Unsafe daemon log file")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    return path


def daemon_log_tail() -> str:
    """Bounded non-following read; return no log if it is not a private regular file."""
    path = log_path()
    if not path.exists():
        return "No private daemon log yet. Enable failure logging; it takes effect on the next server start."
    reject_symlink_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("Daemon log must be a private, user-owned regular file")
        os.lseek(fd, max(0, info.st_size - MAX_TAIL), os.SEEK_SET)
        text = os.read(fd, MAX_TAIL).decode("utf-8", "replace")
        if info.st_size > MAX_TAIL:
            text = text.partition("\n")[2]
        # Redaction shared with journal output; raw text never stored by the app.
        from backend import redact
        return redact(text).strip() or "Private daemon log is empty; waiting for the next server start."
    finally:
        os.close(fd)


def logging_enabled() -> bool:
    from backend import APP_DIR
    unit = unit_dir() / 'sync-webdav.service'
    dropins = unit_dir() / 'sync-webdav.service.d'
    paths = [unit] + sorted(dropins.glob('*.conf'))
    starts, pres, umask = [], [], ''
    try:
        for path in paths:
            if path.is_symlink() or not path.is_file():
                return False
            section = ''
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith(('#', ';')):
                    continue
                if line.startswith('['):
                    section = line
                    continue
                if section != '[Service]' or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                if key == 'ExecStart':
                    if value: starts.append(value)
                    else: starts.clear()
                elif key == 'ExecStartPre':
                    if value: pres.append(value)
                    else: pres.clear()
                elif key == 'UMask':
                    umask = value
        def args(command):
            return shlex.split(command.replace('%%', '%').replace('$$', '$'))
        if len(starts) != 1 or umask not in {'0077', '077'}:
            return False
        command = args(starts[0])
        index = command.index('--log-file')
        if index + 1 >= len(command) or command[index + 1] != str(log_path()):
            return False
        return any(args(pre) == ['/usr/bin/python3', str(APP_DIR / 'log_session.py')] for pre in pres)
    except (OSError, ValueError):
        return False


def mark_server_start() -> None:
    """ExecStartPre marker timestamps otherwise-undated vendor error lines."""
    path = prepare_log()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.write(fd, f"\n[sync-desktop-start {time.time():.6f}]\n".encode())
    finally:
        os.close(fd)


def daemon_auth_cause(text: str, exit_monotonic: int) -> str:
    """Read only the server attempt containing this boot's active-service exit.

    Later retry markers cannot overwrite an initial expired verdict. Unmarked
    or rotated-away segments are displayed as logs but not guessed as evidence.
    """
    if exit_monotonic <= 0:
        return ""
    exit_wall = time.time() - time.monotonic() + exit_monotonic / 1_000_000
    matches = list(re.finditer(r"(?m)^\[sync-desktop-start ([0-9]+\.[0-9]+)\]$", text))
    for index, match in enumerate(matches):
        start = float(match.group(1))
        next_start = float(matches[index + 1].group(1)) if index + 1 < len(matches) else float('inf')
        if start <= exit_wall < next_start:
            segment = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
            return original_auth_cause([{'MESSAGE': line} for line in segment.splitlines()])
    return ""


def auth_observation(entries: list[dict]) -> str:
    """Latest meaningful auth outcome; vendor unknown+revoked is not proof of revocation."""
    observation = ""
    unknown = False
    current_invocation = ""
    for entry in entries:
        unit = entry.get("_SYSTEMD_USER_UNIT", "")
        if unit not in {"", "sync-webdav.service"}:
            continue
        message = entry.get("MESSAGE", "").lower()
        invocation = entry.get("_SYSTEMD_INVOCATION_ID", "")
        if invocation and current_invocation and invocation != current_invocation:
            unknown = False
        if invocation:
            current_invocation = invocation
        if "starting sync-webdav.service" in message or "sync webdav server v" in message:
            unknown = False
        if "started sync-webdav.service" in message:
            observation, unknown = "", False
        elif "sdk session check failed" in message:
            observation, unknown = "unknown", True
        elif "auth rejected" in message:
            match = re.search(r"auth rejected:\s*(expired|revoked|missing|invalid)", message)
            if match:
                reason = match.group(1)
                observation = "unknown" if reason == "revoked" and unknown else reason
            else:
                observation = "unknown"
        elif any(word in message for word in ("address already in use", "no stored session", "no saved session")):
            observation = ""
    return observation


def original_auth_cause(entries: list[dict]) -> str:
    """First auth verdict in the bounded window around the last running-server exit."""
    segment = []
    for entry in entries:
        segment.append(entry)
        message = entry.get("MESSAGE", "").lower()
        if "auth rejected" in message:
            return auth_observation(segment)
    return auth_observation(segment)


def failure_window(exit_monotonic: int) -> tuple[str, str] | None:
    if exit_monotonic <= 0:
        return None
    # systemd timestamps are CLOCK_MONOTONIC usecs in this boot. Reconstruct a
    # short wall-clock window to preserve the first rejection despite retries.
    wall = time.time() - time.monotonic() + exit_monotonic / 1_000_000
    if wall > time.time() + 5:
        return None
    return tuple(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall + delta))
                 for delta in (-2, 90))


def legacy_logging_dropin(binary: Path) -> str:
    """Preserve a simple documented legacy command, rejecting custom overrides."""
    from setup_engine import systemd_quote
    from backend import APP_DIR
    source = unit_dir() / "sync-webdav.service"
    reject_symlink_path(source)
    starts = [line.partition("=")[2] for line in source.read_text().splitlines()
              if line.startswith("ExecStart=")]
    if len(starts) != 1 or any(c in starts[0] for c in ("\\", "$", "\n")):
        raise ValueError("Custom WebDAV service command cannot be changed safely; adopt it through Connect / configure")
    args = shlex.split(starts[0].replace("%h", str(Path.home())))
    if any("%" in arg for arg in args) or not args or args[0] != str(binary) or "--daemon" not in args:
        raise ValueError("Only the documented local daemon service can receive this logging override")
    allowed = {"--daemon": None, "--verbose": None, "--allow-from": "none", "--port": None,
               "--profile": None, "--mount": None, "--label": None, "--analytics": None}
    index = 1
    while index < len(args):
        flag = args[index]
        if flag not in allowed:
            raise ValueError("Custom WebDAV arguments require explicit setup adoption before logging can be configured")
        if flag not in {"--daemon", "--verbose"}:
            if index + 1 >= len(args) or (allowed[flag] and args[index + 1] != allowed[flag]):
                raise ValueError("Unsupported WebDAV service arguments")
            index += 1
        index += 1
    args += ["--log-file", str(log_path())]
    command = " ".join(systemd_quote(arg) for arg in args)
    marker = ' '.join(systemd_quote(arg) for arg in ['/usr/bin/python3', str(APP_DIR / 'log_session.py')])
    return f"{LOG_MARKER}\n[Service]\nUMask=0077\nExecStartPre={marker}\nExecStart=\nExecStart={command}\n"
