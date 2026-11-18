#!/usr/bin/python3
"""Build or inspect the generic noarch RPM, offline and without installing it.

Python 3.8+ standard library only. Building additionally requires rpmbuild and
rpm (4.14+); checking an existing artifact requires only rpm.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_helper(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BUILD = load_helper("sync_desktop_rpm_source", "build.py")
RELEASE = load_helper("sync_desktop_rpm_release", "release_notes.py")
INSTALLER = BUILD.INSTALLER
PACKAGE = INSTALLER.PACKAGE
APP_ID = INSTALLER.APP_ID
RPM_RELEASE = "1"
REQUIRES = (
    "python3 >= 3.8",
    "/usr/bin/python3",
    "(python3-gobject-base or python3-gobject)",
    "(gtk3 >= 3.16 or typelib-1_0-Gtk-3_0 >= 3.16)",
    "(libayatana-appindicator-gtk3 or typelib-1_0-AyatanaAppIndicator3-0_1)",
    "polkit", "systemd", "util-linux", "xdg-utils", "desktop-file-utils",
)
RECOMMENDS = ("curl", "rclone", "fuse3", "kmod")
# These RPM format features are all readable by rpm 4.14. Do not silently accept
# a newer payload format or interpreter/ELF ABI dependency from the build host.
RPMLIB_REQUIRES = {
    "rpmlib(CompressedFileNames) <= 3.0.4-1",
    "rpmlib(FileDigests) <= 4.6.0-1",
    "rpmlib(PayloadFilesHavePrefix) <= 4.0-1",
    "rpmlib(RichDependencies) <= 4.12.0-1",
}
CACHE_SCRIPT = """if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -f /usr/share/icons/hicolor/index.theme ]; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor || true
fi
exit 0"""
METADATA_FORMAT = "\\n".join("%{" + tag + "}" for tag in (
    "NAME", "VERSION", "RELEASE", "ARCH", "LICENSE", "PAYLOADFORMAT",
    "PAYLOADCOMPRESSOR", "FILEDIGESTALGO")) + "\\n"
FILES_FORMAT = ("[%{FILENAMES}\\t%{FILEMODES:octal}\\t%{FILEUSERNAME}\\t"
                "%{FILEGROUPNAME}\\t%{FILELINKTOS}\\t%{FILEDIGESTS}\\n]")


def installed_version(root: Path = ROOT, supplied: Optional[str] = None) -> str:
    """Use the source literal reader, never import settings or the GUI."""
    version = RELEASE.read_version(root / "install.py")
    if RELEASE.read_version(root / "settings.py") != version:
        raise ValueError("settings.py VERSION does not match install.py VERSION")
    if supplied is not None:
        if not re.fullmatch(RELEASE.VERSION_PATTERN, supplied):
            raise ValueError("--version must be a literal MAJOR.MINOR.PATCH version")
        if supplied != version:
            raise ValueError("--version does not match the installed source version")
    if root == ROOT and version != INSTALLER.VERSION:
        raise ValueError("Installer helper VERSION does not match source literal")
    return version


def source_epoch() -> int:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", str(BUILD.DEFAULT_EPOCH)))
    if not 0 <= epoch <= 0xFFFFFFFF:
        raise ValueError("SOURCE_DATE_EPOCH must be between 0 and 4294967295")
    return epoch


def artifact_name(version: str) -> str:
    return "{}-{}-{}.noarch.rpm".format(PACKAGE, version, RPM_RELEASE)


def payload_files(root: Path = ROOT) -> Dict[str, BUILD.Member]:
    """An input-only allowlist: dynamic public runtime modules and icons."""
    for directory in ("packaging", "docs"):
        if (root / directory).is_symlink():
            raise ValueError("RPM input directory must not be a symlink: " + str(root / directory))
    files = {}

    def add(destination: str, source: Path, mode: int = 0o644) -> None:
        if source.is_symlink() or not source.is_file():
            raise ValueError("RPM inputs must be regular files: " + str(source))
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", destination) or ".." in Path(destination).parts:
            raise ValueError("Unsafe RPM payload path: " + destination)
        files[destination] = BUILD.Member(source.read_bytes(), mode)

    for path in INSTALLER.runtime_files(root):
        add("/usr/share/{}/{}".format(PACKAGE, path.relative_to(root).as_posix()), path)
    add("/usr/bin/" + PACKAGE, root / "packaging" / PACKAGE, 0o755)
    add("/usr/share/applications/" + APP_ID + ".desktop",
        root / "packaging" / (APP_ID + ".desktop"))
    add("/usr/share/icons/hicolor/scalable/apps/" + APP_ID + ".svg",
        root / "icons/sync-webdav-online.svg")
    for source in ("README.md", "docs/DISTRIBUTION.md", "docs/CHANGELOG.md", "docs/RPM.md"):
        add("/usr/share/doc/{}/{}".format(PACKAGE, Path(source).name), root / source)
    add("/usr/share/licenses/{}/LICENSE".format(PACKAGE), root / "LICENSE")
    return files


def owned_directories(files: Dict[str, BUILD.Member]):
    # Own application-specific directories, never shared system directories.
    bases = ("/usr/share/" + PACKAGE, "/usr/share/doc/" + PACKAGE,
             "/usr/share/licenses/" + PACKAGE)
    directories = set(bases)
    for name in files:
        directories.update(str(parent) for parent in Path(name).parents
                           if any(str(parent).startswith(base + "/") for base in bases))
    return directories


def file_manifest(files: Dict[str, BUILD.Member]) -> str:
    lines = ["%defattr(-,root,root,-)"]
    lines.extend("%dir " + name for name in sorted(owned_directories(files)))
    for name, member in sorted(files.items()):
        flag = "%license " if name.startswith("/usr/share/licenses/") else (
            "%doc " if name.startswith("/usr/share/doc/") else "")
        lines.append("%attr({:04o},root,root) {}{}".format(member.mode, flag, name))
    return "\n".join(lines)


def render_spec(version: str, epoch: int, files: Dict[str, BUILD.Member]) -> str:
    if not re.fullmatch(RELEASE.VERSION_PATTERN, version):
        raise ValueError("Invalid RPM version")
    template = ROOT / "packaging/sync-webdav-desktop.spec.in"
    if template.is_symlink() or not template.is_file():
        raise ValueError("RPM spec template must be a regular file")
    result = template.read_text(encoding="utf-8")
    values = {"VERSION": version, "EPOCH": str(epoch), "FILES": file_manifest(files),
              "REQUIRES": "\n".join("Requires: " + item for item in REQUIRES),
              "RECOMMENDS": "\n".join("Recommends: " + item for item in RECOMMENDS),
              "CACHE_SCRIPT": CACHE_SCRIPT}
    for name, value in values.items():
        result = result.replace("@" + name + "@", value)
    if re.search(r"@[A-Z_]+@", result):
        raise ValueError("Unexpanded RPM spec template")
    return result


def stage_sources(topdir: Path, version: str, epoch: int, files: Dict[str, BUILD.Member]) -> Path:
    """Stage only install assets, not a developer checkout or the installer."""
    for directory in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS", "HOME"):
        (topdir / directory).mkdir()
    prefix = PACKAGE + "-" + version
    members = {"payload" + name: member for name, member in files.items()}
    archive = BUILD.gzip_bytes(BUILD.tar_bytes(members, epoch, prefix), epoch)
    (topdir / "SOURCES" / (prefix + ".tar.gz")).write_bytes(archive)
    spec = topdir / "SPECS" / (PACKAGE + ".spec")
    spec.write_text(render_spec(version, epoch, files), encoding="utf-8")
    return spec


def prerequisite(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ValueError("Missing prerequisite: {} (RPM 4.14+ tools; no host packages were installed)".format(name))
    return path


def run(command, **kwargs) -> str:
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, **kwargs)
    return result.stdout


def normalized(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def check_artifact(path: Path, version: str, files: Dict[str, BUILD.Member], rpm: Optional[str] = None) -> None:
    """Query only: never install/extract the RPM or execute package scriptlets."""
    rpm = rpm or prerequisite("rpm")
    path = path.resolve()
    if path.name != artifact_name(version) or not path.is_file():
        raise ValueError("Expected existing artifact " + artifact_name(version))

    # Check the package digests as well as the header file digests; an altered
    # or truncated compressed payload must not pass a header-only inspection.
    run([rpm, "--noplugins", "--checksig", "--nosignature", "--", str(path)],
        env=dict(os.environ, LC_ALL="C"))

    def query(*arguments):
        return run([rpm, "--noplugins", "-qp", *arguments, "--", str(path)],
                   env=dict(os.environ, LC_ALL="C"))

    metadata = query("--qf", METADATA_FORMAT).splitlines()
    expected = [PACKAGE, version, RPM_RELEASE, "noarch", "MIT", "cpio", "gzip", "8"]
    if metadata != expected:
        raise ValueError("Unexpected RPM metadata/payload format: " + repr(metadata))
    entries = {}
    for line in query("--qf", FILES_FORMAT).splitlines():
        fields = line.split("\t")
        if len(fields) != 6 or fields[0] in entries:
            raise ValueError("Malformed or duplicate RPM file entry")
        name, mode, user, group, link, digest = fields
        if user != "root" or group != "root" or link:
            raise ValueError("Unsafe RPM ownership or symlink: " + name)
        entries[name] = (int(mode, 8), digest)
    directories = owned_directories(files)
    if set(entries) != set(files) | directories:
        raise ValueError("RPM contains missing or unexpected files (installer/cache/vendor/secrets are forbidden)")
    for name, member in files.items():
        mode, digest = entries[name]
        if mode != stat.S_IFREG | member.mode or digest != hashlib.sha256(member.data).hexdigest():
            raise ValueError("Unexpected RPM content/mode: " + name)
    for name in directories:
        if entries[name][0] != stat.S_IFDIR | 0o755:
            raise ValueError("Unexpected RPM directory mode: " + name)
    requires = {line.strip() for line in query("--requires").splitlines() if line.strip()}
    if not set(REQUIRES) <= requires or requires - set(REQUIRES) - {"/bin/sh"} - RPMLIB_REQUIRES:
        raise ValueError("Unexpected/missing RPM dependency (host ABI pins are forbidden): " + repr(sorted(requires)))
    recommends = set(query("--recommends").splitlines())
    if recommends != set(RECOMMENDS):
        raise ValueError("Unexpected RPM recommendations: " + repr(sorted(recommends)))
    expected_scripts = ("postinstall scriptlet (using /bin/sh):\n" + CACHE_SCRIPT
                        + "\npostuninstall scriptlet (using /bin/sh):\n" + CACHE_SCRIPT)
    if normalized(query("--scripts")) != normalized(expected_scripts):
        raise ValueError("RPM scriptlets are not limited to the approved desktop cache refreshes")
    # Triggers are separate from --scripts on some RPM releases.
    trigger_format = "".join("%|" + tag + "?{[%{" + tag + "}]}:{}|" for tag in (
        "TRIGGERSCRIPTS", "FILETRIGGERSCRIPTS", "TRANSFILETRIGGERSCRIPTS"))
    if query("--qf", trigger_format).strip():
        raise ValueError("Unexpected RPM triggers")


def build_rpm(output_dir: Path, version: str, epoch: int) -> Path:
    rpmbuild, rpm = prerequisite("rpmbuild"), prerequisite("rpm")
    files = payload_files()
    # A controlled Linux /tmp path keeps RPM macro expansion independent of
    # hostile cwd/output/TMPDIR paths. Caller paths never enter shell/spec text.
    with tempfile.TemporaryDirectory(prefix="sync-webdav-rpm-", dir="/tmp") as directory:
        topdir = Path(directory)
        spec = stage_sources(topdir, version, epoch, files)
        environment = dict(os.environ, HOME=str(topdir / "HOME"),
                           XDG_CONFIG_HOME=str(topdir / "HOME/.config"),
                           SOURCE_DATE_EPOCH=str(epoch), TZ="UTC", LC_ALL="C")
        command = [rpmbuild, "--noplugins", "-bb", "--define", "_topdir " + str(topdir),
                   "--define", "_buildhost reproducible.invalid", str(spec)]
        run(command, cwd=str(topdir), env=environment)
        artifact = topdir / "RPMS/noarch" / artifact_name(version)
        check_artifact(artifact, version, files, rpm)
        output_dir.mkdir(parents=True, exist_ok=True)
        result = output_dir / artifact.name
        BUILD.output_file(result, artifact.read_bytes())
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", help="Optional literal version; must match install.py and settings.py")
    parser.add_argument("--check-only", nargs="?", const=True, type=Path, metavar="RPM",
                        help="Inspect an existing RPM without building (default: output-dir artifact)")
    args = parser.parse_args(argv)
    try:
        version = installed_version(supplied=args.version)
        if args.check_only is not None:
            artifact = args.output_dir / artifact_name(version) if args.check_only is True else args.check_only
            check_artifact(artifact, version, payload_files())
            print("Validated " + str(artifact))
        else:
            artifact = build_rpm(args.output_dir, version, source_epoch())
            print("Built and validated " + str(artifact))
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as exc:
        detail = (exc.stderr or exc.stdout) if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        parser.exit(1, "RPM packaging failed: {}\n".format(detail or str(exc)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
