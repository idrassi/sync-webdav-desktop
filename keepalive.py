"""Optional read-only cloud keep-alive; never traverses a FUSE mount or stores listings."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from authentication import Cancelled, terminate_group
from backend import OperationError, Snapshot, collect_snapshot, operation_lock
from settings import Settings, reject_symlink_path


class KeepAliveSkipped(RuntimeError):
    """Offline, busy or superseded settings: not a failed connection."""


@dataclass(frozen=True)
class KeepAliveResult:
    state: str  # success, skipped, failed, cancelled
    message: str
    completed_at: str
    history_error: str = ''


def ensure_local_remote(settings: Settings) -> None:
    """Only use the intended local WebDAV remote; do not expose credentials in errors."""
    path = settings.config_file
    try:
        reject_symlink_path(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OperationError('Keep-alive requires a private, user-owned Rclone configuration')
            if info.st_size > 1024 * 1024:
                raise OperationError('Rclone configuration is too large for safe keep-alive validation')
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_file(stream)
        section = parser[settings.remote]
        if (section.get('type') != 'webdav' or section.get('url', '').rstrip('/') != f'http://127.0.0.1:{settings.port}'
                or section.get('user') != 'sync' or not section.get('pass')):
            raise OperationError('Keep-alive remote does not match the configured local Sync WebDAV endpoint')
    except (OSError, ValueError, configparser.Error, KeyError):
        raise OperationError('Cannot validate the private local Rclone configuration for keep-alive') from None


def read_remote(settings: Settings, cancel: threading.Event, timeout: float = 25) -> None:
    """One shallow PROPFIND via a fresh Rclone client, avoiding mount listing caches.

    Listing output is sent to /dev/null. No file contents, directories, names or
    credentials are saved in GUI logs. No retries beyond this bounded request.
    """
    binary = shutil.which('rclone', path=os.defpath)
    if not binary:
        raise OperationError('Rclone is unavailable for keep-alive')
    args = [binary, 'lsf', settings.remote + ':/', '--config=' + str(settings.config_file),
            '--max-depth=1', '--dirs-only', '--log-level=ERROR', '--retries=1',
            '--low-level-retries=1', '--contimeout=5s', '--timeout=15s', '--disable=ListR']
    env = {key: value for key, value in os.environ.items() if not key.startswith('RCLONE_')}
    env.update(LC_ALL='C', HOME=str(Path.home()))
    if cancel.is_set():
        raise Cancelled('Keep-alive cancelled')
    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    deadline = time.monotonic() + timeout
    try:
        while child.poll() is None:
            if cancel.wait(0.2):
                raise Cancelled('Keep-alive cancelled')
            if time.monotonic() >= deadline:
                raise OperationError('Keep-alive read timed out; no automatic sign-in or restart was performed')
        if child.returncode:
            raise OperationError('Keep-alive read failed; check service diagnostics (no automatic sign-in or restart)')
    finally:
        terminate_group(child)


def ping(settings: Settings, cancel: threading.Event,
         status_reader: Callable = collect_snapshot) -> KeepAliveResult:
    """Serialize with recovery and verify real state again immediately before read."""
    from activity_history import record_attempt, session_issued_at
    attempt = str(uuid.uuid4())
    issued = ''
    started = None
    history_error = ''
    def result(state, message):
        nonlocal history_error
        if started is not None:
            try:
                record_attempt(state, settings, attempt, issued, time.monotonic() - started)
            except (OSError, ValueError):
                history_error = 'Activity history could not be written; read outcome is still valid'
        return KeepAliveResult(state, message, time.strftime('%H:%M:%S'), history_error)
    if not settings.keepalive_enabled or cancel.is_set():
        return result('cancelled' if cancel.is_set() else 'skipped', 'Keep-alive is disabled or cancelled')
    entered = False
    try:
        with operation_lock(shared=True):
            entered = True
            if cancel.is_set():
                raise Cancelled('Keep-alive cancelled')
            snapshot = status_reader(settings)
            if snapshot.state != 'online' or snapshot.stale_mount:
                return result('skipped', 'Paused: drive is not connected')
            ensure_local_remote(settings)
            started = time.monotonic()
            try:
                issued = session_issued_at(settings)
                record_attempt('started', settings, attempt, issued)
            except (OSError, ValueError):
                history_error = 'Activity history could not be written; read outcome is still valid'
            read_remote(settings, cancel)
            return result('success', 'Read-only Sync keep-alive succeeded')
    except Cancelled:
        return result('cancelled', 'Keep-alive cancelled')
    except OperationError:
        # The lock is intentionally nonblocking. An operation elsewhere must
        # win, and failed reads must not leak CLI/config error details.
        return result('failed', 'Keep-alive could not complete; check connection status') if entered else result('skipped', 'Paused during connection management')
    except OSError:
        return result('failed', 'Keep-alive command could not be started')


class KeepAliveSchedule:
    """Monotonic cadence with no overlap, catch-up storms, or stale result updates."""
    def __init__(self, clock: Callable = time.monotonic):
        self.clock = clock
        self.signature = None
        self.interval = 60
        self.enabled = True
        self.next_due = 0.0
        self.online = False
        self.generation = 0
        self.pending = None
        self.last_result: KeepAliveResult | None = None

    def configure(self, settings: Settings) -> bool:
        signature = (settings.keepalive_enabled, settings.keepalive_interval_seconds,
                     settings.profile, settings.remote, settings.port, settings.mount_point,
                     str(settings.config_file))
        if signature == self.signature:
            return False
        self.signature = signature
        self.enabled = settings.keepalive_enabled
        self.interval = settings.keepalive_interval_seconds
        self.generation += 1
        self.pending = None
        self.online = False
        self.next_due = self.clock() + self.interval
        self.last_result = None
        return True

    def claim(self, snapshot: Snapshot | None, busy: bool) -> int | None:
        now = self.clock()
        ready = bool(self.enabled and snapshot and snapshot.state == 'online' and not snapshot.stale_mount and not busy)
        if not ready:
            self.online = False
            self.next_due = now + self.interval
            return None
        if not self.online:
            self.online = True
            self.next_due = now + self.interval
        if self.pending is not None or now < self.next_due:
            return None
        self.pending = self.generation
        self.next_due = now + self.interval  # At most one read after suspend.
        return self.pending

    def complete(self, generation: int, result: KeepAliveResult) -> bool:
        if generation != self.generation or self.pending != generation:
            return False
        self.pending = None
        self.last_result = result
        # Wait a full interval after completion even when a slow call overlapped
        # several schedule ticks. Never hammer an expired or unavailable server.
        self.next_due = self.clock() + self.interval
        return True

    def pause(self) -> None:
        self.generation += 1
        self.pending = None
        self.online = False
        self.next_due = self.clock() + self.interval

    def description(self, snapshot: Snapshot | None, busy: bool) -> str:
        if not self.enabled:
            return 'Disabled'
        if busy:
            return 'Paused during connection management'
        if not snapshot or snapshot.state != 'online' or snapshot.stale_mount:
            return 'Paused while disconnected'
        if self.pending is not None:
            return 'Read-only keep-alive in progress…'
        prefix = f'Enabled · every {self.interval} seconds'
        if self.last_result:
            prefix += f' · last {self.last_result.state} at {self.last_result.completed_at}'
            if self.last_result.history_error:
                prefix += ' · history unavailable'
        else:
            prefix += ' · waiting for first read'
        return prefix
