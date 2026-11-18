# Ubuntu and Debian package compatibility

## Why one `.deb` works

`sync-webdav-desktop_<version>_all.deb` contains Python source, shell launchers and
images. It does not contain compiled application binaries, bundled Python, GTK,
or Sync's proprietary WebDAV executable. There is no build-host glibc ABI to carry
from the Ubuntu 24.04 runner into older systems. The host uses its own interpreter
and GUI libraries.

The package baseline is:

- Python **3.8+**, using postponed annotations and APIs available at that baseline.
- GTK **3.16+**, PyGObject and `gir1.2-ayatanaappindicator3-0.1`.
- systemd user services, util-linux, xdg-utils and desktop-file-utils.
- `pkexec | policykit-1`: Ubuntu 20.04/Debian 11 provide `/usr/bin/pkexec` in
  policykit-1; newer releases provide a separate pkexec package.
- Rclone, FUSE 3, curl and kmod are installed by setup when required/approved.

## Distribution targets

| Target | Stock Python family | Package target |
| --- | --- | --- |
| Ubuntu 20.04 LTS | 3.8 | Same universal `.deb`; legacy OS security support required |
| Ubuntu 22.04 LTS | 3.10 | Same universal `.deb` |
| Ubuntu 24.04 LTS | 3.12 | Same universal `.deb` |
| Ubuntu 26.04 LTS | 3.14 | Same universal `.deb` |
| Debian 11 Bullseye | 3.9 | Same universal `.deb`; legacy OS security support required |
| Debian 12 Bookworm | 3.11 | Same universal `.deb` |
| Debian 13 Trixie | 3.13 | Same universal `.deb` |

Other releases with these dependencies may also work, but are not automatically
covered by this matrix. Ubuntu 18.04/Python 3.6 and Debian 10/Python 3.7 are below
the supported baseline. Future distributions can be added without generating a
second package if dependencies remain available.

`Architecture: all` describes this application package, not arbitrary Sync server
support: Sync supplies x86_64/ARM64 executables. The vendor's runtime/library
requirements and account behavior remain separate from the companion's package.
The application also requires a graphical desktop, browser, a systemd user
session, and a PolicyKit authentication agent for administrator actions. Ubuntu
needs the enabled AppIndicators extension; other desktops need a status-notifier
host. Package installation alone cannot verify those runtime conditions.

## CI validation

The release workflow builds **once on Ubuntu 24.04** and uploads one `.deb`. Seven
compatibility jobs download that identical artifact and use Docker images:
`ubuntu:20.04`, `ubuntu:22.04`, `ubuntu:24.04`, `ubuntu:26.04`, `debian:bullseye`,
`debian:bookworm`, and `debian:trixie`.

Each job resolves package dependencies through that distribution's repositories,
installs the `.deb`, runs tests with stock Python, verifies the desktop entry and
Rclone read/credential flags, and performs a GTK startup/presentation smoke test
using Xvfb and an isolated session bus as a non-root user. The source is mounted
read-only and copied into the disposable container. There is no host-home access,
real cloud account, vendor installation, live FUSE mount or authentication.

This is packaging/interpreter/GUI-startup validation, not full systemd boot or
cloud acceptance testing. A failed mirror/image download is an infrastructure
failure and must not be reported as application compatibility success. Publication
waits for every configured target job to pass.

For local validation (Docker required), build first, then run:

```sh
/usr/bin/python3 packaging/build.py
docker run --rm \
  --volume "$PWD:/workspace:ro" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env COMPATIBILITY_SOURCE_DIR=/tmp/source \
  ubuntu:20.04 sh -c \
  'mkdir -p /tmp/source; cp -a /workspace/. /tmp/source/; cd /tmp/source; sh scripts/check_deb_compatibility.sh'
```

**Do not run `scripts/check_deb_compatibility.sh` on your workstation.** It installs
packages and creates a test user, and is intended only for disposable containers.
The full packaging checker runs separately on the build runner: some older
`desktop-file-validate` releases reject pathological quote/backslash fixture
paths even though installed system launchers use simple fixed paths. Compatibility
jobs validate the actual installed desktop entry rather than treating that old
validator's fixture behavior as a package incompatibility.

Use `docs/ACCEPTANCE_TESTS.md` on real/VM desktops to validate vendor sign-in,
FUSE, reboot persistence, tray-host behavior and ARM64. Prefer supported OS
releases; compatibility with a legacy OS is not an endorsement of unpatched use.
