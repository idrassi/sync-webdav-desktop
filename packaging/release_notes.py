#!/usr/bin/python3
"""Validate a public release tag and extract its changelog notes, offline."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "idrassi/sync-webdav-desktop"
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


def tag_version(tag: str) -> str:
    if not re.fullmatch(r"v?" + VERSION_PATTERN, tag):
        raise ValueError("Release tags must be vMAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH, e.g. v1.0.0")
    return tag[1:] if tag.startswith("v") else tag


def read_version(path: Path) -> str:
    """Read a literal VERSION without executing application or installer code."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                raise ValueError(f"{path.name}: VERSION must be a string literal")
            values.append(node.value.value)
    if len(values) != 1 or not re.fullmatch(VERSION_PATTERN, values[0]):
        raise ValueError(f"{path.name}: expected exactly one MAJOR.MINOR.PATCH VERSION assignment")
    return values[0]


def changelog_entry(changelog: str, version: str) -> str:
    """Accept '## 1.0.0 — Title', '## [1.0.0] - Date' or a plain version heading."""
    heading = re.compile(r"^##[ \t]+\[?v?(" + VERSION_PATTERN + r")\]?(?=[ \t]|$)", re.MULTILINE)
    matches = [match for match in heading.finditer(changelog) if match.group(1) == version]
    if len(matches) != 1:
        raise ValueError(f"docs/CHANGELOG.md must contain exactly one '## {version}' release entry")
    match = matches[0]
    section_start = changelog.find("\n", match.start())
    if section_start < 0:
        raise ValueError(f"Changelog entry for {version} contains no changes")
    next_heading = re.search(r"^##[ \t]+", changelog[section_start + 1:], re.MULTILINE)
    end = section_start + 1 + next_heading.start() if next_heading else len(changelog)
    section = changelog[section_start + 1:end].strip()
    # Reject a section consisting only of headings, comments or link definitions.
    meaningful = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    meaningful = "\n".join(line for line in meaningful.splitlines()
                           if not line.lstrip().startswith("#") and not re.match(r"^\[[^]]+\]:", line))
    if not meaningful.strip():
        raise ValueError(f"Changelog entry for {version} contains no changes")
    return section


def release_notes(tag: str, root: Path = ROOT) -> tuple[str, str]:
    version = tag_version(tag)
    for filename in ("settings.py", "install.py"):
        actual = read_version(root / filename)
        if actual != version:
            raise ValueError(f"Tag {tag} does not match {filename} VERSION={actual}")
    changes = changelog_entry((root / "docs/CHANGELOG.md").read_text(encoding="utf-8"), version)
    body = f"""# Sync WebDAV Desktop {version}

Independent, unofficial Linux desktop application for Sync.com WebDAV.

## Changes

{changes}

## Downloads and installation

- **`sync-webdav-desktop_{version}_all.deb`** — one Debian/Ubuntu-family package
  with a Python 3.8+/GTK 3.16+ baseline. CI checks this same artifact on Ubuntu
  20.04/22.04/24.04/26.04 and Debian 11/12/13. `Architecture: all` contains the
  Python application; x86_64/ARM64 support also requires a compatible vendor
  WebDAV server. Container checks do not validate real cloud authentication.
- **`sync-webdav-desktop-{version}.tar.gz`** — source distribution and per-user installer.
- **`SHA256SUMS`** — SHA-256 checksums for both assets.

Install the Debian package with:

```bash
sudo apt install ./sync-webdav-desktop_{version}_all.deb
```

Or extract the source archive and follow the
[installation instructions](https://github.com/{REPOSITORY}/blob/{tag}/README.md#install).
Requires Linux, Python 3.8+, systemd, GTK/AppIndicator support, and browser sign-in.
See the [distribution guide](https://github.com/{REPOSITORY}/blob/{tag}/docs/DISTRIBUTION.md)
for desktop requirements, privileges, and platform validation limitations.

MIT license covers this application only. Sync's proprietary WebDAV server is
**not included** and is downloaded from the vendor during setup. This project is
not affiliated with or endorsed by Sync.com.
"""
    return version, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, help="Optional GitHub Actions step output file")
    args = parser.parse_args(argv)
    try:
        version, notes = release_notes(args.tag)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(notes, encoding="utf-8")
        if args.github_output:
            # Both values are validated single-line literals; no untrusted shell
            # or GitHub expression interpolation is used to convey tag metadata.
            with args.github_output.open("a", encoding="utf-8") as stream:
                stream.write(f"version={version}\ntag={args.tag}\n")
        print(f"Validated release {args.tag}; notes written to {args.output}")
    except (OSError, ValueError, SyntaxError) as exc:
        parser.exit(1, f"Release validation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
