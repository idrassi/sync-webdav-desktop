"""Non-secret desktop settings and discovery of the documented legacy setup."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

VERSION = "1.0.1"
HOME = Path.home()
APP_NAME = "Sync WebDAV Desktop"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "sync-webdav-desktop"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "systemd/user"


def reject_symlink_path(path: Path) -> None:
    probe = path
    while probe != probe.parent:
        if probe.is_symlink():
            raise ValueError(f"Symbolic links are not allowed in managed file paths: {probe}")
        probe = probe.parent


def private_config_dir() -> Path:
    path = config_dir()
    reject_symlink_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("Settings directory is not owned by this user")
    path.chmod(0o700)
    return path


def atomic_write(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    reject_symlink_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"Refusing to replace symbolic link: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".sync-desktop-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def is_within(path: Path, parent: Path) -> bool:
    """Lexical containment, available on Python 3.8; does not follow symlinks."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_text(value: str, label: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{label} contains invalid control characters")
    return value


@dataclass(frozen=True)
class Settings:
    mount_point: str = str(HOME / "sync-drive")
    content: str = "files"
    folder: str = ""
    port: int = 4918
    profile: str = "default"
    remote: str = "sync-webdav"
    rclone_config: str = ""
    start_services: bool = True
    before_login: bool = False
    tray_autostart: bool = True
    bookmark: bool = True
    managed: bool = False
    configured: bool = False
    keepalive_enabled: bool = True
    keepalive_interval_seconds: int = 60
    installer_sha256: str = ""

    @property
    def config_file(self) -> Path:
        return Path(self.rclone_config) if self.rclone_config else config_dir() / f"rclone-{self.profile}.conf"

    @property
    def scope(self) -> str:
        return "vault/" if self.content == "vault" else self.folder.strip("/") + "/" if self.content == "folder" else ""

    def validate(self) -> Settings:
        safe_text(self.mount_point, "Mount location")
        try:
            path = Path(self.mount_point).expanduser()
        except RuntimeError as exc:
            raise ValueError("Mount location refers to an unknown home directory") from exc
        if ".." in path.parts:
            raise ValueError("Mount location must not contain .. path components")
        if not path.is_absolute() or path == Path("/") or path == HOME:
            raise ValueError("Choose an absolute mount folder, not / or your home folder itself")
        if not is_within(path, HOME):
            raise ValueError("For safety, choose a mount folder inside your home directory")
        # Reject symlink components, even if they point into HOME.
        probe = path
        while probe != HOME:
            if probe.is_symlink():
                raise ValueError("Mount paths containing symbolic links are not supported")
            probe = probe.parent
        if path == config_dir() or is_within(config_dir(), path) or is_within(path, config_dir()):
            raise ValueError("The mount cannot contain the application's settings directory")
        if any(part.startswith(".") for part in path.relative_to(HOME).parts):
            raise ValueError("Do not mount inside hidden application/configuration directories")
        if self.content not in ("files", "vault", "folder"):
            raise ValueError("Unknown content selection")
        safe_text(self.folder, "Sync folder")
        if self.content == "folder":
            # As in scope, normalize leading/trailing slashes; reject empty
            # segments in the path actually passed to the vendor.
            if any(part in {"", ".", ".."} for part in self.folder.strip("/").split("/")):
                raise ValueError("Enter a relative Sync folder path without empty, . or .. components")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("Choose a local port from 1024 to 65535")
        for name, label in [(self.profile, "Profile"), (self.remote, "Rclone remote")]:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
                raise ValueError(f"{label} must contain only letters, digits, underscores or hyphens")
        safe_text(self.rclone_config, "Rclone configuration path")
        if ".." in self.config_file.parts:
            raise ValueError("Rclone configuration path must not contain ..")
        if not self.config_file.is_absolute() or not is_within(self.config_file, HOME):
            raise ValueError("Rclone configuration must be inside your home directory")
        if self.config_file == path or is_within(self.config_file, path):
            raise ValueError("Rclone configuration must be outside the mounted folder")
        reserved = {settings_path(), unit_dir() / 'sync-webdav.service', unit_dir() / 'sync-rclone.service'}
        if self.config_file in reserved or is_within(self.config_file, unit_dir()):
            raise ValueError("Rclone configuration must not overwrite settings or systemd service files")
        if is_within(self.config_file, config_dir() / 'credential-recovery') or is_within(self.config_file, config_dir() / 'backups'):
            raise ValueError("Rclone configuration must not overwrite recovery records or backups")
        for name in ("start_services", "before_login", "tray_autostart", "bookmark", "managed", "configured", "keepalive_enabled"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"Invalid boolean setting: {name}")
        if type(self.keepalive_interval_seconds) is not int or not 30 <= self.keepalive_interval_seconds <= 3600:
            raise ValueError("Keep-alive interval must be from 30 to 3600 seconds")
        if self.before_login and not self.start_services:
            raise ValueError("Pre-login startup requires automatic service startup")
        if not isinstance(self.installer_sha256, str) or (self.installer_sha256 and
                not re.fullmatch(r"[0-9a-fA-F]{64}", self.installer_sha256)):
            raise ValueError("Installer SHA-256 must be empty or 64 hexadecimal digits")
        return replace(self, mount_point=str(path), installer_sha256=self.installer_sha256.lower())

    def to_dict(self) -> dict:
        return asdict(self)


def save_settings(settings: Settings) -> None:
    settings = settings.validate()
    private_config_dir()
    atomic_write(settings_path(), json.dumps(settings.to_dict(), indent=2) + "\n")


def discover_legacy() -> Settings:
    """Inspect service command paths/options only; do not read credentials."""
    webdav = unit_dir() / "sync-webdav.service"
    rclone = unit_dir() / "sync-rclone.service"
    if not webdav.is_file() or not rclone.is_file():
        return Settings()
    result = Settings(configured=True, rclone_config=str(
        Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "rclone/rclone.conf"))
    try:
        def exec_args(path):
            for line in path.read_text().splitlines():
                if line.startswith("ExecStart="):
                    return shlex.split(line.split("=", 1)[1].replace("%h", str(HOME)).replace("%%", "%"))
            return []
        args = exec_args(webdav)
        updates = {}
        for key in ("port", "profile", "mount"):
            flag = "--" + key
            if flag in args and args.index(flag) + 1 < len(args):
                value = args[args.index(flag) + 1]
                if key == "port": updates["port"] = int(value)
                elif key == "profile": updates["profile"] = value
                elif value.strip("/") == "vault": updates["content"] = "vault"
                else: updates.update(content="folder", folder=value.strip("/"))
        args = exec_args(rclone)
        if "mount" in args:
            index = args.index("mount")
            updates["remote"] = args[index + 1].split(":", 1)[0]
            updates["mount_point"] = args[index + 2]
            for arg in args:
                if arg.startswith("--config="): updates["rclone_config"] = arg.split("=", 1)[1]
        return replace(result, **updates).validate()
    except (ValueError, IndexError, OSError):
        # Unsupported custom commands are not automatically rewritten.
        return result


def load_settings() -> Settings:
    try:
        source = settings_path().read_text()
    except FileNotFoundError:
        return discover_legacy()
    value = json.loads(source)
    if not isinstance(value, dict):
        raise ValueError("Invalid settings file")
    allowed = Settings.__dataclass_fields__
    return Settings(**{key: val for key, val in value.items() if key in allowed}).validate()
