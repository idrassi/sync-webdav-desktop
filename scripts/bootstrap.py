#!/usr/bin/python3
"""Check source-install prerequisites; ask before using sudo/package managers.

This helper does not download Sync, create services, authenticate, or install the
app. Use install.py separately as your normal desktop user after prerequisites
are available. The application may offer optional runtime tools via pkexec.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

PROBE = """
import sys
if sys.version_info < (3, 8):
    raise SystemExit('Python 3.8 or newer is required')
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, AyatanaAppIndicator3
if Gtk.get_minor_version() < 16:
    raise SystemExit('GTK 3.16 or newer is required')
"""
GI_PACKAGES = {
    "apt-get": ["python3-gi", "gir1.2-gtk-3.0", "gir1.2-ayatanaappindicator3-0.1"],
    "dnf": ["python3-gobject", "gtk3", "libayatana-appindicator-gtk3"],
    "pacman": ["python-gobject", "gtk3", "libayatana-appindicator"],
}
TOOLS = {"systemctl": "systemd", "findmnt": "util-linux",
         "update-desktop-database": "desktop-file-utils", "xdg-open": "xdg-utils", "pkexec": "pkexec"}


def missing_prerequisites() -> tuple[bool, list[str]]:
    python = Path("/usr/bin/python3")
    if not python.is_file():
        return True, [tool for tool in TOOLS if not shutil.which(tool)]
    try:
        result = subprocess.run([str(python), "-c", PROBE], capture_output=True,
                                text=True, timeout=20)
        gi_missing = result.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        gi_missing = True
    return gi_missing, [tool for tool in TOOLS if not shutil.which(tool)]


def package_manager() -> str | None:
    # Prefer the distro's own manager even if another is also installed.
    identifiers = []
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "ID_LIKE"}:
                identifiers.extend(shlex.split(value.strip())[0].split())
    except (OSError, ValueError, IndexError):
        pass
    for identifier in identifiers:
        manager = {"debian": "apt-get", "ubuntu": "apt-get", "linuxmint": "apt-get",
                   "fedora": "dnf", "rhel": "dnf", "centos": "dnf",
                   "arch": "pacman", "manjaro": "pacman"}.get(identifier)
        if manager and shutil.which(manager):
            return manager
    return next((name for name in GI_PACKAGES if shutil.which(name)), None)


def authorization_package(manager: str) -> str:
    if manager != 'apt-get':
        return 'polkit'
    candidate = subprocess.run(['apt-cache', 'show', 'pkexec'], capture_output=True, text=True)
    return 'pkexec' if candidate.returncode == 0 and 'Package: pkexec' in candidate.stdout else 'policykit-1'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report prerequisites only; never invoke sudo or change packages")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.exit(1, "Sync WebDAV Desktop requires Linux, systemd, and a graphical desktop.\n")
    gi_missing, tools = missing_prerequisites()
    if not gi_missing and not tools:
        print("System Python >=3.8, GTK 3, Ayatana AppIndicator, pkexec, and desktop/system tools are ready.")
        print("Next: /usr/bin/python3 install.py (run as your desktop user, without sudo).")
        return 0
    if gi_missing:
        print("Missing: system Python >=3.8 and/or its GI, GTK 3, Ayatana AppIndicator typelibs.")
    if tools:
        print(f"Missing commands: {', '.join(tools)}")
    manager = package_manager()
    if manager is None:
        parser.exit(1, "No supported package manager found. Install prerequisites manually; see docs/DISTRIBUTION.md.\n")
    packages = (["python3" if manager != "pacman" else "python", *GI_PACKAGES[manager]]
                if gi_missing else [])
    for tool in tools:
        package = TOOLS[tool]
        if tool == 'pkexec':
            # Focal/Bullseye ship pkexec inside policykit-1; newer releases split it.
            package = authorization_package(manager)
        packages.append(package)
    operation = [manager, "-S", "--needed"] if manager == "pacman" else [manager, "install"]
    command = ["sudo", *operation, *dict.fromkeys(packages)]
    print(f"Proposed administrator command:\n  {shlex.join(command)}")
    print("No Sync binary, authentication, profiles, user services, or cloud files are handled here.")
    if args.check:
        return 1
    if os.geteuid() == 0:
        parser.exit(1, "Run this helper as your normal desktop user, not as root.\n")
    if not shutil.which("sudo"):
        parser.exit(1, "sudo is unavailable. Ask your administrator to install the listed packages.\n")
    if not sys.stdin.isatty():
        parser.exit(1, "An interactive terminal is required for confirmation; no packages were changed.\n")
    try:
        confirmed = input("Install these distribution packages with sudo? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; no packages were changed.")
        return 1
    if confirmed not in {"y", "yes"}:
        print("Declined; no packages were changed.")
        return 1
    try:
        result = subprocess.run(command, check=False)
    except (OSError, KeyboardInterrupt) as exc:
        parser.exit(1, f"Package installation interrupted or unavailable: {exc}\n")
    if result.returncode:
        print("Package installation did not complete. Review the package manager's output.")
        return 1
    gi_missing, tools = missing_prerequisites()
    if gi_missing or tools:
        parser.exit(1, "Some prerequisites remain missing (Python must be >=3.8). See docs/DISTRIBUTION.md.\n")
    print("Prerequisites ready. Run /usr/bin/python3 install.py without sudo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
