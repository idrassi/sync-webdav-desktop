"""Exact, read-only mount inspection and explicitly requested stale-mount cleanup.

Monitoring must only call inspect_mount. Cleanup is for a caller that has stopped
both Sync services and holds its recovery/operation lock; it never stops services,
kills processes, or requests administrator privileges. A MainPID-based monitoring
classification is not proof that a mount is safe to detach.

Linux offers no atomic "unmount this mount ID only" operation through fusermount.
Rechecking mount identity and process ownership immediately before each command
narrows, but cannot eliminate, races with other processes creating/replacing a
mount. The caller's lock must serialize all of this application's mount changes.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Callable, TYPE_CHECKING

from backend import OperationError, SYSTEMCTL, redact, run_command

if TYPE_CHECKING:
    from settings import Settings

PROC_ROOT = Path("/proc")
RCLONE_UNIT = "sync-rclone.service"
COMMAND_TIMEOUT = 10


@dataclass(frozen=True)
class MountInfo:
    mount_id: int
    parent_id: int
    device: str
    root: str
    target: str
    mount_options: tuple[str, ...]
    optional_fields: tuple[str, ...]
    fstype: str
    source: str
    super_options: tuple[str, ...]

    @property
    def user_id(self) -> int | None:
        """Return the FUSE owner only when a single valid user_id is supplied."""
        owners = [option.partition("=")[2] for option in self.super_options
                  if option.partition("=")[0] == "user_id"]
        if len(owners) != 1 or not re.fullmatch(r"[0-9]+", owners[0]):
            return None
        return int(owners[0])


class StaleMountBusy(OperationError):
    """A normal stale-mount unmount needs closed handles or explicit consent."""

    def __init__(self, mount_point: str):
        self.mount_point = mount_point
        super().__init__(
            f"The stale Sync mount at {mount_point} is busy. Close Nautilus and other "
            "file managers, terminals, and open file handles using the drive, then retry. "
            "If it remains busy, rerun with explicit lazy-detach confirmation "
            "(allow_lazy=True). Lazy detach leaves existing handles disconnected; "
            "it is never allowed while an owning rclone process is live.")


def _unescape(value: str) -> str:
    # Decode once, as filesystem bytes: \\134040 means a literal \\040, not a space.
    def replace(match: re.Match[bytes]) -> bytes:
        number = int(match[1], 8)
        if number == 0 or number > 255:
            raise ValueError("Invalid mountinfo octal escape")
        return bytes([number])
    return os.fsdecode(re.sub(rb"\\([0-7]{3})", replace, os.fsencode(value)))


def parse_mountinfo(text: str) -> list[MountInfo]:
    """Parse /proc/self/mountinfo, retaining order, flags and propagation fields.

    Malformed input is an error, not evidence that a mount is absent. Fields are
    split before octal unescaping, so spaces/tabs/newlines in names stay intact.
    """
    mounts = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            fields = line.split()
            separator = fields.index("-", 6)
            if separator < 6 or len(fields) != separator + 4:
                raise ValueError("Invalid field count")
            mount_id, parent_id = int(fields[0]), int(fields[1])
            if mount_id <= 0 or parent_id < 0 or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
                raise ValueError("Invalid mount identity")
            root, target = _unescape(fields[3]), _unescape(fields[4])
            # nsfs (e.g. Snap namespace bind mounts) legitimately uses a
            # synthetic root such as mnt:[4026532954]. It is not a filesystem
            # path. Targets must still be absolute; owned FUSE roots below
            # are checked separately before any cleanup.
            if not target.startswith("/"):
                raise ValueError("Mount targets must be absolute")
            mounts.append(MountInfo(
                mount_id, parent_id, fields[2], root, target,
                tuple(_unescape(option) for option in fields[5].split(",")),
                tuple(fields[6:separator]), fields[separator + 1],
                _unescape(fields[separator + 2]),
                tuple(_unescape(option) for option in fields[separator + 3].split(","))))
        except (ValueError, IndexError, OverflowError) as exc:
            raise OperationError("Cannot safely parse /proc/self/mountinfo.") from exc
    return mounts


def _lexical_target(settings: Settings) -> str:
    raw = os.fspath(settings.mount_point)
    if not isinstance(raw, str) or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise OperationError("Unsafe mount point: invalid characters.")
    path = Path(raw)
    if path.anchor != "/" or ".." in path.parts:
        raise OperationError("Unsafe mount point: use an absolute path without '..'.")
    # Path collapses redundant separators and '.', but never resolves symlinks.
    return str(path)


def _mounts_at(settings: Settings) -> list[MountInfo]:
    target = _lexical_target(settings)
    try:
        text = (PROC_ROOT / "self/mountinfo").read_text(
            encoding="utf-8", errors="surrogateescape")
    except OSError as exc:
        raise OperationError("Cannot read /proc/self/mountinfo; mount state is unknown.") from exc
    return [mount for mount in parse_mountinfo(text) if mount.target == target]


def inspect_mount(settings: Settings) -> MountInfo | None:
    """Inspect only the exact lexical mount point, without stat or FUSE access.

    For monitoring, the last matching mountinfo entry is reported when mounts are
    stacked. This ordering is not an ownership/topology guarantee: cleanup rejects
    *all* multiple-entry cases instead of relying on which entry is visible.
    """
    entries = _mounts_at(settings)
    return entries[-1] if entries else None


def _single_mount(settings: Settings) -> MountInfo | None:
    entries = _mounts_at(settings)
    if len(entries) > 1:
        raise OperationError("Refusing cleanup: ambiguous/stacked mounts at the Sync mount point.")
    return entries[0] if entries else None


def _root_source(source: str, remote: str) -> bool:
    return source.rstrip("/") == remote + ":"


def _assert_owned_mount(mount: MountInfo, settings: Settings) -> None:
    if mount.root != "/":
        raise OperationError("Refusing cleanup: the mount is not the full configured Rclone root filesystem.")
    if mount.fstype != "fuse.rclone":
        raise OperationError(f"Refusing cleanup: foreign filesystem {mount.fstype} at {mount.target}.")
    if not _root_source(mount.source, settings.remote):
        raise OperationError("Refusing cleanup: the mount source is not the configured rclone remote root.")
    if mount.user_id != os.getuid():
        raise OperationError("Refusing cleanup: the FUSE owner is foreign, missing, or ambiguous.")


def _reject_symlink_components(target: str) -> None:
    """Check ancestors first; never resolve or enumerate a possibly dead mount.

    lstat does not follow an endpoint symlink. A disconnected FUSE endpoint can
    return ENOTCONN even to lstat; only that endpoint error is allowed, after all
    ancestors were verified. Its exact mounted identity is checked separately.
    """
    path = Path(target)
    for probe in [*reversed(path.parents), path]:
        try:
            info = os.lstat(probe)
        except OSError as exc:
            if probe == path and exc.errno == errno.ENOTCONN:
                continue
            raise OperationError(f"Cannot safely inspect mount path component {probe}.") from exc
        if stat.S_ISLNK(info.st_mode):
            raise OperationError(f"Refusing cleanup: symbolic link in mount path: {probe}.")
        if not stat.S_ISDIR(info.st_mode):
            raise OperationError(f"Refusing cleanup: mount path component is not a directory: {probe}.")


def _assert_service_stopped() -> None:
    result = run_command([
        SYSTEMCTL, "--user", "show", RCLONE_UNIT, "--no-pager",
        "--property=LoadState,ActiveState,SubState,MainPID,ControlPID"], timeout=5)
    if result.returncode:
        raise OperationError("Cannot establish rclone service state; cleanup is refused.")
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise OperationError("Unknown/ambiguous rclone service state; cleanup is refused.")
        fields[key] = value
    required = {"LoadState", "ActiveState", "SubState", "MainPID", "ControlPID"}
    if not required.issubset(fields) or fields["LoadState"] != "loaded":
        raise OperationError("Unknown rclone service state; cleanup is refused.")
    if (fields["ActiveState"], fields["SubState"]) not in {
            ("inactive", "dead"), ("failed", "failed")}:
        raise OperationError("The rclone service is live or transitioning; stop services before cleanup.")
    for key in ("MainPID", "ControlPID"):
        if not re.fullmatch(r"[0-9]+", fields[key]):
            raise OperationError("Unknown rclone service process metadata; cleanup is refused.")
        if int(fields[key]) != 0:
            raise OperationError("The rclone service still has a live/control process; cleanup is refused.")


def _process_gone(directory: Path, exc: OSError) -> bool:
    if exc.errno not in {errno.ENOENT, errno.ESRCH}:
        return False
    try:
        directory.stat()
    except OSError as check:
        return check.errno in {errno.ENOENT, errno.ESRCH}
    return False


def _command_targets_mount(args: list[str], settings: Settings, directory: Path) -> bool:
    """Conservatively recognize rclone mount operands, including interspersed flags.

    We consider all non-option arguments after the remote to avoid needing a
    version-specific catalogue of rclone flags. Option values can produce false
    positives (a safe refusal), but cannot conceal an exact target operand.
    """
    if Path(args[0]).name != "rclone":
        return False
    commands = [i for i, arg in enumerate(args[1:], 1) if arg in {"mount", "cmount"}]
    if not commands:
        return False
    operands = args[commands[0] + 1:]
    sources = [i for i, arg in enumerate(operands) if _root_source(arg, settings.remote)]
    if not sources:
        return False
    target = _lexical_target(settings)
    candidates = [arg for arg in operands[sources[0] + 1:] if arg and not arg.startswith("-")]
    if not candidates:
        raise OperationError("Cannot determine a same-user rclone mount target; cleanup is refused.")
    # First check every absolute operand; no filesystem lookup is necessary.
    if any(os.path.normpath(arg) == target for arg in candidates if os.path.isabs(arg)):
        return True
    relative = [arg for arg in candidates if not os.path.isabs(arg)]
    if relative:
        try:
            cwd = os.readlink(directory / "cwd")
        except OSError as exc:
            raise OperationError("Cannot read rclone process working directory; cleanup is refused.") from exc
        if not os.path.isabs(cwd) or cwd.endswith(" (deleted)"):
            raise OperationError("Unknown rclone process working directory; cleanup is refused.")
        if any(os.path.normpath(os.path.join(cwd, arg)) == target for arg in relative):
            return True
    return False


def _assert_no_live_rclone(settings: Settings) -> None:
    _assert_service_stopped()
    uid = os.getuid()
    try:
        directories = list(PROC_ROOT.iterdir())
    except OSError as exc:
        raise OperationError("Cannot scan process metadata; cleanup is refused.") from exc
    for directory in directories:
        if not directory.name.isascii() or not directory.name.isdecimal():
            continue
        try:
            # proc directories identify ordinary foreign users without requiring
            # their restricted metadata. Undumpable processes are root-owned in
            # proc, even when their real UID is ours: always inspect those Uids
            # rather than assuming that root directory ownership means foreign.
            directory_uid = directory.stat().st_uid
            if directory_uid not in {uid, 0}:
                continue
            status = (directory / "status").read_text(encoding="utf-8", errors="surrogateescape")
            fields = {}
            for line in status.splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    if key in fields:
                        raise OperationError("Ambiguous process metadata; cleanup is refused.")
                    fields[key] = value.strip()
            owners = fields.get("Uid", "").split()
            if len(owners) != 4 or any(not re.fullmatch(r"[0-9]+", owner) for owner in owners):
                raise OperationError("Unknown process ownership; cleanup is refused.")
            if uid not in {int(owner) for owner in owners}:
                continue
            command = (directory / "cmdline").read_bytes()
            if not command:
                # Zombies/exiting processes cannot own a working FUSE connection.
                # Kernel threads are positively identified, not inferred from argv.
                if fields.get("State", "").split()[:1] in [["Z"], ["X"], ["x"]] or fields.get("Kthread") == "1":
                    continue
                raise OperationError("Unreadable/empty live process command line; cleanup is refused.")
            if not command.endswith(b"\0"):
                raise OperationError("Incomplete process command line; cleanup is refused.")
            args = [os.fsdecode(arg) for arg in command[:-1].split(b"\0")]
            if not args[0]:
                raise OperationError("Unknown process command; cleanup is refused.")
            if _command_targets_mount(args, settings, directory):
                raise OperationError("A live same-user rclone mount owns this target; cleanup is refused.")
        except OSError as exc:
            if _process_gone(directory, exc):
                continue
            raise OperationError("Inaccessible process metadata; cleanup is refused.") from exc
        except UnicodeError as exc:
            raise OperationError("Unreadable process metadata; cleanup is refused.") from exc


def _identity(mount: MountInfo) -> tuple:
    return (mount.mount_id, mount.parent_id, mount.device, mount.root, mount.target,
            mount.fstype, mount.source, mount.user_id)


def _revalidate(settings: Settings, expected: MountInfo) -> None:
    current = _single_mount(settings)
    if current is None or _identity(current) != _identity(expected):
        raise OperationError("The mount identity changed or disappeared; no further unmount was attempted.")
    _assert_owned_mount(current, settings)
    _reject_symlink_components(current.target)


def _find_fusermount() -> str:
    # Prefer the distribution helper and avoid a user-controlled PATH. Both FUSE
    # versions accept POSIX '--', '-u' and '-u -z'; neither needs '-f' or sudo.
    preferred = "/usr/bin/fusermount3"
    if os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    for name in ("fusermount3", "fusermount"):
        helper = shutil.which(name, path=os.defpath)
        if helper:
            return helper
    raise OperationError("No fusermount3/fusermount helper found; install the distribution FUSE package.")


def _verify_absent(settings: Settings) -> None:
    if _mounts_at(settings):
        raise OperationError("Unmount reported success, but an exact mount entry remains or was replaced.")


def cleanup_stale_mount(settings: Settings, allow_lazy: bool = False,
                        progress: Callable[[str], None] | None = None) -> bool:
    """Unmount only an owned, provably non-live leftover; return whether removed.

    No mount returns False without commands. Normal unmount is always attempted
    first. Lazy detach is permitted only for a busy normal failure, explicit
    allow_lazy=True, and fresh checks of the SAME mount and absence of live owners.
    Command errors/timeouts never implicitly authorize a lazy or forced unmount.
    """
    if type(allow_lazy) is not bool:
        raise OperationError("Lazy detach requires an explicit boolean confirmation.")
    target = _lexical_target(settings)
    if target == "/" or target == str(Path.home()):
        raise OperationError("Refusing cleanup of / or the home directory itself.")
    if not isinstance(settings.remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", settings.remote):
        raise OperationError("Invalid configured rclone remote; cleanup is refused.")
    mount = _single_mount(settings)
    if mount is None:
        return False
    _assert_owned_mount(mount, settings)
    _reject_symlink_components(target)
    helper = _find_fusermount()
    if progress:
        progress("Checking that the stopped Sync mount has no live rclone owner, then unmounting…")
    _assert_no_live_rclone(settings)
    _revalidate(settings, mount)
    result = run_command([helper, "-u", "--", target], timeout=COMMAND_TIMEOUT)
    if result.returncode == 0:
        _verify_absent(settings)
        return True
    detail = redact(result.stderr or result.stdout).strip()[-2000:]
    if not re.search(r"\bbusy\b", detail, re.IGNORECASE):
        raise OperationError(detail or "Normal stale-mount unmount failed; no lazy detach attempted.")
    if not allow_lazy:
        raise StaleMountBusy(target)
    if progress:
        progress("Normal unmount was busy. Rechecking ownership before the explicitly confirmed lazy detach…")
    _assert_no_live_rclone(settings)
    _revalidate(settings, mount)
    result = run_command([helper, "-u", "-z", "--", target], timeout=COMMAND_TIMEOUT)
    if result.returncode:
        detail = redact(result.stderr or result.stdout).strip()[-2000:]
        raise OperationError(detail or "Lazy stale-mount detach failed.")
    _verify_absent(settings)
    return True
