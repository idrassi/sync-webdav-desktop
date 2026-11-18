"""Bounded private session/keep-alive evidence. No raw CLI, filenames or tokens.

Only schema-allowlisted timestamps, opaque attempt IDs, result codes and cadence
are retained. Reading session.json extracts issuedAt only; no account identity or
authentication token is exposed in records or reports. Collection is opt-in to
using the application's keep-alive and local diagnostics, never telemetry.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import stat
import time
import uuid

from settings import Settings, config_dir, private_config_dir, reject_symlink_path

MAX_BYTES = 256 * 1024
MAX_SESSION_BYTES = 64 * 1024
EVENTS = {'started', 'success', 'failed', 'cancelled', 'skipped', 'session_expired'}
ALLOWED_FIELDS = {'event', 'at', 'epoch', 'attempt', 'session_issued_at', 'interval_seconds',
                  'duration_seconds', 'server_exit_at', 'server_exit_epoch', 'server_exit_monotonic', 'boot_id'}


def utc_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='milliseconds')


def timestamp(value) -> float | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def history_path() -> Path:
    return config_dir() / 'logs' / 'keepalive-history.jsonl'


def _check_file(fd: int, writable=False) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or (not writable and info.st_mode & 0o077)):
        raise ValueError('History/session evidence requires a private user-owned regular file')
    if writable:
        os.fchmod(fd, 0o600)


def session_issued_at(settings: Settings) -> str:
    base = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    path = base / 'sync-webdav' / 'profiles' / settings.profile / 'session.json'
    if not path.exists():
        return ''
    reject_symlink_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        _check_file(stream.fileno())
        if os.fstat(stream.fileno()).st_size > MAX_SESSION_BYTES:
            raise ValueError('Session metadata exceeds safe size')
        try:
            data = json.loads(stream.read(MAX_SESSION_BYTES))
        except ValueError:
            return ''
    value = data.get('issuedAt') if isinstance(data, dict) else None
    epoch = timestamp(value)
    return utc_time(epoch) if epoch is not None else ''


@contextmanager
def _history_lock():
    private_config_dir()
    directory = history_path().parent
    reject_symlink_path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid():
        raise ValueError('History directory is not owned by this user')
    directory.chmod(0o700)
    lock = directory / '.keepalive-history.lock'
    reject_symlink_path(lock)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _check_file(fd, writable=True)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_file(path: Path) -> bytes:
    if not path.exists():
        return b''
    reject_symlink_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        _check_file(stream.fileno())
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - MAX_BYTES))
        data = stream.read(MAX_BYTES)
        return data.partition(b'\n')[2] if size > MAX_BYTES else data


def _valid(record: dict) -> bool:
    if not isinstance(record, dict) or set(record) - ALLOWED_FIELDS or record.get('event') not in EVENTS:
        return False
    if timestamp(record.get('at')) is None or not isinstance(record.get('epoch'), (float, int)):
        return False
    if not math.isfinite(record['epoch']) or abs(timestamp(record['at']) - record['epoch']) > 1:
        return False
    if record.get('session_issued_at') and timestamp(record['session_issued_at']) is None:
        return False
    if not isinstance(record.get('session_issued_at', ''), str):
        return False
    if 'attempt' in record:
        try:
            if str(uuid.UUID(record['attempt'])) != record['attempt']:
                return False
        except (ValueError, TypeError, AttributeError):
            return False
    if 'interval_seconds' in record and (type(record['interval_seconds']) is not int or not 30 <= record['interval_seconds'] <= 3600):
        return False
    if 'duration_seconds' in record and (not isinstance(record['duration_seconds'], (float, int)) or
                                        not math.isfinite(record['duration_seconds']) or record['duration_seconds'] < 0):
        return False
    if 'server_exit_at' in record and timestamp(record['server_exit_at']) is None:
        return False
    if 'server_exit_epoch' in record and (not isinstance(record['server_exit_epoch'], (float, int)) or not math.isfinite(record['server_exit_epoch'])):
        return False
    if 'server_exit_monotonic' in record and (type(record['server_exit_monotonic']) is not int or record['server_exit_monotonic'] <= 0):
        return False
    if 'boot_id' in record and (not isinstance(record['boot_id'], str) or not re.fullmatch(r'[0-9a-f-]{36}', record['boot_id'])):
        return False
    if record['event'] == 'session_expired':
        if not {'server_exit_at', 'server_exit_epoch', 'server_exit_monotonic', 'boot_id'}.issubset(record):
            return False
        if abs(timestamp(record['server_exit_at']) - record['server_exit_epoch']) > 1:
            return False
        if not record.get('session_issued_at') or record['server_exit_epoch'] < timestamp(record['session_issued_at']):
            return False
    return True


def _decode(data: bytes) -> list[dict]:
    records = []
    for line in data.splitlines():
        try:
            record = json.loads(line)
            if _valid(record):
                records.append(record)
        except (ValueError, TypeError, OverflowError):
            continue
    return records


_decoded_history = (None, ())


def _history_records() -> tuple[dict, ...]:
    global _decoded_history
    # Always recheck private-file safety, including on a cache hit. Cache
    # only validated records, not raw input or the time-dependent report.
    path = history_path()
    rotated, current = _read_file(path.with_suffix('.jsonl.1')), _read_file(path)
    key = (hashlib.sha256(rotated).digest(), hashlib.sha256(current).digest())
    cached_key, records = _decoded_history
    if key != cached_key:
        ordered = sorted(_decode(rotated) + _decode(current), key=lambda record: record['epoch'])
        records = tuple(ordered)
        _decoded_history = (key, records)
    return records


def read_history() -> list[dict]:
    return [record.copy() for record in _history_records()]


def append(record: dict, unique=False) -> None:
    if not _valid(record):
        raise ValueError('Invalid activity record; arbitrary details are not logged')
    data = (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode()
    if len(data) > 2048:
        raise ValueError('Activity record exceeds safe size')
    with _history_lock():
        if unique and record['event'] == 'session_expired':
            if any(r['event'] == 'session_expired' and r.get('session_issued_at') == record.get('session_issued_at')
                   and r.get('server_exit_monotonic') == record.get('server_exit_monotonic')
                   and r.get('boot_id') == record.get('boot_id') for r in _history_records()):
                return
        path = history_path()
        backup = path.with_suffix('.jsonl.1')
        reject_symlink_path(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            _check_file(fd, writable=True)
            if os.fstat(fd).st_size + len(data) > MAX_BYTES:
                os.close(fd)
                fd = -1
                reject_symlink_path(backup)
                if backup.exists():
                    check = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    try: _check_file(check)
                    finally: os.close(check)
                os.replace(path, backup)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if fd >= 0: os.close(fd)


def record_attempt(event: str, settings: Settings, attempt: str, issued: str,
                   duration: float | None = None) -> None:
    now = time.time()
    record = {'event': event, 'at': utc_time(now), 'epoch': now, 'attempt': attempt,
              'session_issued_at': issued, 'interval_seconds': settings.keepalive_interval_seconds}
    if duration is not None:
        record['duration_seconds'] = round(max(0, duration), 3)
    append(record)


def server_exit_epoch(snapshot) -> float | None:
    monotonic = snapshot.webdav.active_exit_monotonic
    if monotonic <= 0 or snapshot.webdav.running:
        return None
    value = time.time() - time.monotonic() + monotonic / 1_000_000
    return value if value <= time.time() + 5 else None


def observe_expiry(settings: Settings, snapshot) -> None:
    if snapshot.auth_reason != 'expired':
        return
    issued = snapshot.session_issued_at
    if not isinstance(issued, str) or not issued or session_issued_at(settings) != issued:
        return  # A newer sign-in superseded this collected snapshot.
    exit_epoch = server_exit_epoch(snapshot)
    if not issued or exit_epoch is None or exit_epoch < timestamp(issued):
        return
    # Whole-second exit key is stable despite small wall/monotonic conversion drift.
    exit_epoch = int(exit_epoch)
    now = time.time()
    append({'event': 'session_expired', 'at': utc_time(now), 'epoch': now,
            'session_issued_at': issued, 'server_exit_at': utc_time(exit_epoch),
            'server_exit_epoch': exit_epoch, 'server_exit_monotonic': snapshot.webdav.active_exit_monotonic,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}, unique=True)


def _local(value: str) -> str:
    epoch = timestamp(value)
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec='seconds') if epoch is not None else 'unknown'


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f'{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s'


def report(settings: Settings, snapshot) -> dict:
    """Observation only: no unverified claim of a configured TTL or inactivity policy."""
    issued = session_issued_at(settings)
    all_records = _history_records()
    records = [r for r in all_records if issued and r.get('session_issued_at') == issued]
    successes = [r for r in records if r['event'] == 'success']
    failures = [r for r in records if r['event'] == 'failed']
    starts = [r for r in records if r['event'] == 'started']
    completed_attempts = {r.get('attempt') for r in records if r['event'] in {'success', 'failed', 'cancelled', 'skipped'}}
    incomplete = [r for r in starts if r.get('attempt') not in completed_attempts]
    exit_epoch = server_exit_epoch(snapshot)
    prior_expiries = [r for r in records if r['event'] == 'session_expired']
    if snapshot.auth_reason != 'expired' or not issued or (exit_epoch is not None and exit_epoch < timestamp(issued)):
        exit_epoch = None
    if exit_epoch is None and prior_expiries:
        exit_epoch = prior_expiries[-1]['server_exit_epoch']
    lines = ['Session sign-in: ' + (_local(issued) if issued else 'timestamp unavailable')]
    if issued:
        end = exit_epoch or time.time()
        lines.append(('Observed expiry-to-server-exit age: ' if exit_epoch else 'Current session age: ') + _duration(end - timestamp(issued)))
    lines.append(f'Recorded reads in this session: {len(successes)} succeeded, {len(failures)} failed, {len(starts)} started (bounded retained history).')
    if incomplete:
        lines.append(f'{len(incomplete)} recorded read starts have no retained completion (interrupted, in progress, or rotated history).')
    if successes:
        last = successes[-1]
        lines.append('Last successful read: ' + _local(last['at']))
        if exit_epoch and last['epoch'] <= exit_epoch:
            lines.append('Time from last successful read to server exit: ' + _duration(exit_epoch - last['epoch']))
    else:
        lines.append('No successful reads recorded for this session. Reads before history collection began cannot be reconstructed.')
    if failures:
        lines.append('Last failed read: ' + _local(failures[-1]['at']))
    if issued and successes:
        gaps = [b['epoch'] - a['epoch'] for a, b in zip(successes, successes[1:])]
        if gaps: lines.append('Largest gap between recorded successful reads: ' + _duration(max(gaps)))
    if exit_epoch:
        from session_guidance import AUTO_LOGOUT_HINT, KEEPALIVE_POLICY_NOTE
        lines.append(AUTO_LOGOUT_HINT)
        lines.append(KEEPALIVE_POLICY_NOTE)
        lines.append('Server exit: ' + _local(utc_time(exit_epoch)))
        lines.append('This is observed timing, not the configured timeout. A successful read proves activity, not renewal of the vendor session.')
    recent = records[-20:]
    history_lines = ['Recent activity (local time; no filenames, credentials or CLI output):']
    for record in recent:
        text = _local(record['at']) + '  ' + record['event']
        if 'duration_seconds' in record: text += f"  {record['duration_seconds']:.3f}s"
        if 'interval_seconds' in record: text += f"  interval={record['interval_seconds']}s"
        history_lines.append(text)
    if not recent: history_lines.append('No activity recorded for the current saved session.')
    previous = [r for r in all_records if r['event'] == 'session_expired' and r.get('session_issued_at') != issued]
    if previous:
        history_lines.append('\nPrevious recorded session expiries (server-exit timing, not configured TTL):')
        for record in previous[-5:]:
            start = timestamp(record.get('session_issued_at'))
            if start is not None:
                history_lines.append(_local(record['server_exit_at']) + '  session age ' + _duration(record['server_exit_epoch'] - start))
    return {'session_issued_at': issued, 'server_exit_at': utc_time(exit_epoch) if exit_epoch else '',
            'session_age_seconds': round((exit_epoch or time.time()) - timestamp(issued), 3) if issued else None,
            'successful_reads': len(successes), 'failed_reads': len(failures), 'started_reads': len(starts),
            'last_success_at': successes[-1]['at'] if successes else '',
            'last_failed_at': failures[-1]['at'] if failures else '',
            'summary': '\n'.join(lines), 'history': '\n'.join(history_lines)}
