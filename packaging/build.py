#!/usr/bin/python3
"""Build reproducible source and architecture-independent Debian archives.

Only Python's standard library is required; no git, build backend, dpkg, sudo,
network access, GI imports, proprietary executables, or post-install execution.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Import the installer helpers without leaving a bytecode cache in the checkout.
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("sync_desktop_installer", ROOT / "install.py")
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)
PACKAGE = INSTALLER.PACKAGE
VERSION = INSTALLER.VERSION
APP_ID = INSTALLER.APP_ID
# Stable release timestamp unless the caller explicitly supplies a build epoch.
DEFAULT_EPOCH = 1767225600  # 2026-01-01 00:00:00 UTC


@dataclass(frozen=True)
class Member:
    data: bytes
    mode: int = 0o644


def public_file(path: Path) -> bool:
    return (not path.name.startswith(".")
            and not INSTALLER.PRIVATE_NAME.search(path.name))


def source_files() -> dict[str, Member]:
    """An allowlist, not a recursive snapshot of a developer's working folder.

    Flat public Python modules support future application modules without also
    shipping credentials, runtime directories, downloaded vendor tools, etc.
    New resource formats/directories must be consciously added to this list.
    """
    for directory in ("docs", "packaging", "scripts", "tests"):
        if (ROOT / directory).is_symlink():
            raise ValueError(f"Distribution input directories must not be symlinks: {ROOT / directory}")
    paths = set(INSTALLER.runtime_files(ROOT))
    paths.update(ROOT / name for name in ("install.py", "README.md", "LICENSE", ".gitignore"))
    if (ROOT / "pyproject.toml").exists():
        paths.add(ROOT / "pyproject.toml")
    for pattern in ("docs/*.md", "packaging/*.py", "packaging/*.desktop",
                    "packaging/*.in", "scripts/*.py", "scripts/*.sh", "tests/test_*.py"):
        paths.update(path for path in ROOT.glob(pattern) if public_file(path))
    for name in ("control.in", "copyright", "postinst", "postrm", "sync-webdav-desktop"):
        paths.add(ROOT / "packaging" / name)
    result = {}
    executables = {"install.py", "packaging/build.py", "packaging/check_distribution.py",
                   "packaging/postinst", "packaging/postrm", "packaging/sync-webdav-desktop"}
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Distribution inputs must be regular files: {path}")
        name = path.relative_to(ROOT).as_posix()
        mode = 0o755 if name in executables or name.startswith("scripts/") else 0o644
        result[name] = Member(path.read_bytes(), mode)
    return result


def tar_bytes(files: dict[str, Member], epoch: int, prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        directories = set()
        for name in files:
            full_name = f"{prefix}/{name}" if prefix else name
            directories.update(parent.as_posix() for parent in Path(full_name).parents
                               if parent != Path("."))
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = epoch
            archive.addfile(info)
        for name, member in sorted(files.items()):
            info = tarfile.TarInfo(f"{prefix}/{name}" if prefix else name)
            info.size = len(member.data)
            info.mode = member.mode
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = epoch
            archive.addfile(info, io.BytesIO(member.data))
    return buffer.getvalue()


def gzip_bytes(data: bytes, epoch: int) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=epoch,
                       compresslevel=9) as stream:
        stream.write(data)
    return buffer.getvalue()


def ar_bytes(members: list[tuple[str, bytes]], epoch: int) -> bytes:
    """Debian binary packages are ar containers of three short-named members."""
    result = bytearray(b"!<arch>\n")
    for name, data in members:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name):
            raise ValueError(f"Unsupported ar member name: {name}")
        header = (f"{name + '/':<16}{epoch:<12}{0:<6}{0:<6}"
                  f"{'100644':<8}{len(data):<10}`\n").encode("ascii")
        if len(header) != 60:
            raise ValueError("ar header field overflow")
        result.extend(header)
        result.extend(data)
        if len(data) % 2:
            result.extend(b"\n")
    return bytes(result)


def debian_files(source: dict[str, Member], epoch: int) -> tuple[dict[str, Member], dict[str, Member]]:
    files = {f"usr/share/{PACKAGE}/{path.relative_to(ROOT).as_posix()}":
             source[path.relative_to(ROOT).as_posix()] for path in INSTALLER.runtime_files(ROOT)}
    files[f"usr/bin/{PACKAGE}"] = Member(source[f"packaging/{PACKAGE}"].data, 0o755)
    files[f"usr/share/applications/{APP_ID}.desktop"] = source[f"packaging/{APP_ID}.desktop"]
    files[f"usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg"] = source["icons/sync-webdav-online.svg"]
    doc = f"usr/share/doc/{PACKAGE}"
    files[f"{doc}/copyright"] = source["packaging/copyright"]
    files[f"{doc}/DISTRIBUTION.md"] = source["docs/DISTRIBUTION.md"]
    changelog = (f"{PACKAGE} ({VERSION}) unstable; urgency=medium\n\n"
                 "  * Initial public release of Sync WebDAV Desktop.\n\n"
                 " -- Mounir IDRASSI <mounir.idrassi@amcrypto.jp>  "
                 "Thu, 01 Jan 2026 00:00:00 +0000\n")
    files[f"{doc}/changelog.gz"] = Member(gzip_bytes(changelog.encode(), epoch))
    installed_size = sum((len(member.data) + 1023) // 1024 for member in files.values())
    control = source["packaging/control.in"].data.decode().replace("@VERSION@", VERSION).replace(
        "@INSTALLED_SIZE@", str(installed_size))
    if re.search(r"@[A-Z_]+@", control):
        raise ValueError("Unexpanded control template")
    controls = {"control": Member(control.encode())}
    for name in ("postinst", "postrm"):
        controls[name] = Member(source[f"packaging/{name}"].data, 0o755)
    return controls, files


def output_file(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".build-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--source-only", action="store_true", help="Skip the Debian package")
    args = parser.parse_args(argv)
    try:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", str(DEFAULT_EPOCH)))
        if not 0 <= epoch <= 0xFFFFFFFF:
            raise ValueError("SOURCE_DATE_EPOCH must be between 0 and 4294967295")
        source = source_files()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        name = f"{PACKAGE}-{VERSION}"
        tar_path = args.output_dir / f"{name}.tar.gz"
        output_file(tar_path, gzip_bytes(tar_bytes(source, epoch, prefix=name), epoch))
        print(f"Built {tar_path} ({len(source)} source files)")
        if not args.source_only:
            controls, files = debian_files(source, epoch)
            deb_path = args.output_dir / f"{PACKAGE}_{VERSION}_all.deb"
            output_file(deb_path, ar_bytes([
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", gzip_bytes(tar_bytes(controls, epoch), epoch)),
                ("data.tar.gz", gzip_bytes(tar_bytes(files, epoch), epoch)),
            ], epoch))
            print(f"Built {deb_path} (Architecture: all; {len(files)} payload files)")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Build failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
