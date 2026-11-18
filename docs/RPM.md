# Generic RPM (initial public release 1.0.0)

The RPM asset is **`sync-webdav-desktop-1.0.0-1.noarch.rpm`**. There is one generic
binary RPM, without a Fedora, RHEL or SUSE release suffix. It contains the MIT-licensed
Python desktop application, not Sync.com's proprietary WebDAV server. It is an
independent, unofficial project, not affiliated with or endorsed by Sync.com.

## Build, without installing

From an extracted source release or checkout:

```bash
python3 packaging/build_rpm.py
python3 packaging/build_rpm.py --output-dir /tmp/rpm-output --version 1.0.0
```

Python 3.8+ and the `rpmbuild` and `rpm` tools (RPM 4.14+) are required for
building. The builder uses only the Python standard library and existing local
RPM tools: no git, network, sudo, build backend, dependency installation or host
package installation. Missing tools produce an actionable error. Build in a
disposable Fedora/RHEL/SUSE container or otherwise provision the tools yourself.
It does not use your `~/.rpmmacros`: build HOME and the RPM topdir are temporary
under `/tmp`. Current-working-directory and output paths never enter shell code.

Both `install.py` and `settings.py` must contain the same literal version.
An optional `--version` must match them exactly. Runtime modules and SVG/PNG
icons are discovered using `install.runtime_files`; an allowlisted install-asset
tarball and a rendered spec are staged in the temporary topdir. Only
`rpmbuild -bb` is used; no source RPM is required or emitted. The temporary sources/spec
are removed after completion, and only the binary RPM is copied to `dist/` (or
`--output-dir`), after validation.

## Offline artifact checks

```bash
python3 packaging/build_rpm.py --check-only
python3 packaging/build_rpm.py --check-only /tmp/rpm-output/sync-webdav-desktop-1.0.0-1.noarch.rpm
python3 -m unittest discover -s tests -p test_rpm_packaging.py
```

`--check-only` requires `rpm`, not `rpmbuild`. It verifies package digests with
`rpm --checksig --nosignature` and uses `rpm -qp` to inspect
identity/version/release/noarch/license, gzip/cpio payload format, file names,
ownership, modes and SHA-256 file digests, dependencies, recommendations,
scriptlets and triggers. Expected files and bytes come from the current source;
use the corresponding release source when checking a downloaded RPM. No package
is installed or extracted and no package scriptlet is executed. The helper unit
tests mock RPM tools, so they run without either tool installed.

## Payload and safety boundaries

- `/usr/share/sync-webdav-desktop/`: flat application Python modules and `icons/`,
  matching the Debian payload layout; no installer, tests or vendored binaries.
- `/usr/bin/sync-webdav-desktop`: wrapper using the distribution's
  `/usr/bin/python3`, not a virtual environment.
- `/usr/share/applications/io.github.vera.SyncWebDAVTray.desktop` and the same
  application-ID SVG in `/usr/share/icons/hicolor/scalable/apps/`.
- `/usr/share/doc/sync-webdav-desktop/`: README, distribution guide, changelog,
  and this guide; `/usr/share/licenses/sync-webdav-desktop/LICENSE`: MIT license.

Package post-install/post-uninstall scripts only refresh desktop and icon
caches, best effort. They never configure/start services, create per-user
configuration or autostart entries, download vendor tools, access credentials,
mount drives, or alter home/cloud files. No service/unit/polkit-rule files are
shipped. Launch the application as your normal desktop user, **not as root**;
runtime setup and its explicit authorization remain separate from packaging.

## Dependencies and scope

Required: `python3 >= 3.8`, `/usr/bin/python3`,
`(python3-gobject-base or python3-gobject)`,
`(gtk3 >= 3.16 or typelib-1_0-Gtk-3_0 >= 3.16)`,
`(libayatana-appindicator-gtk3 or typelib-1_0-AyatanaAppIndicator3-0_1)`,
`polkit`, `systemd`, `util-linux`, `xdg-utils` and `desktop-file-utils`.
Recommended: `curl`, `rclone`, `fuse3` and `kmod`. The GTK alternative has the
same 3.16 minimum on both branches. Fixed package-name rich alternatives cater
to Fedora/RHEL 9+ and modern SUSE naming; they are not permissive
library/interpreter path fallbacks. Repository availability (especially Ayatana AppIndicator and rclone)
varies: RHEL may need additional repositories, and some distributions package
the FUSE helper separately. Resolve these through your distribution, not a
forced `rpm --nodeps` installation. Dependency-provider and desktop compatibility
must be verified for each target release; no blanket RPM-distribution support
claim is implied by a generic filename. RHEL 8 and Leap 15 with their default
Python 3.6 are explicitly unsupported: do not silently replace the system
interpreter to force this RPM onto an older distribution.

For an appropriately provisioned Fedora/RHEL system, installation is normally
`sudo dnf install ./sync-webdav-desktop-1.0.0-1.noarch.rpm`; on SUSE use
`sudo zypper install ./sync-webdav-desktop-1.0.0-1.noarch.rpm`. These are deliberate
administrator actions, not commands run by the builder or checker. A noarch
Python payload does not guarantee availability of the vendor WebDAV binary on
all architectures. See `DISTRIBUTION.md` for per-user privileges and desktop
limitations. Container packaging checks do not validate browser authentication,
tray behavior, live mounts, or real cloud operations.

## Release CI and validation

GitHub Actions builds the RPM once in a Fedora 43 container, using a generic
`noarch` spec with no distribution suffix. The same artifact is tested on Fedora
43/44, CentOS Stream 9/10, openSUSE Leap 16.0 and Tumbleweed before publication.
Tests install dependencies through the target package manager, run the source
unit suite as an ordinary user, separately exercise installed runtime modules
outside the checkout, verify package files, and start the installed GUI with
an isolated session bus. CentOS Stream 10 uses headless Weston via
`wlheadless-run` from `xwayland-run` because it does not ship Xvfb; the other
RPM targets use Xvfb. These display tools are CI-only dependencies.
EPEL/CRB are enabled only inside disposable Enterprise Linux test containers;
neither the desktop app nor the RPM enables repositories on users' systems.

The release includes the RPM alongside the universal `.deb`, source archive and
checksums. The public RPM is unsigned: integrity can be checked with release
SHA-256 checksums, but that is not a cryptographic publisher signature. openSUSE
may request confirmation for the unsigned local package; review it deliberately.
Signing RPMs can be added with an explicit key-management policy later.

Local same-artifact package installation, unit tests and GTK smoke checks passed
on Fedora 43, CentOS Stream 9/10 and openSUSE Tumbleweed. A successful CentOS Stream
check is not a claim of full RHEL subscription-repository validation; real RHEL,
ARM64, browser sign-in, FUSE and boot behavior require desktop/VM acceptance tests.
Other matrix targets are validated when CI runs, not presumed from these results.

## Reproducibility and format baseline

`SOURCE_DATE_EPOCH` is an integer from 0 through 4294967295; the default is
1767225600 (2026-01-01 UTC). Staged tar/gzip data has sorted names, fixed modes,
root ownership and epoch timestamps. RPM receives the epoch, mtime-clamping and
buildtime macros plus a fixed build host. Automatic dependency generation,
Python bytecompilation, debug packages and vendor post-processing hooks are
disabled: no current-build-host Python ABI/glibc pins or `.pyc` files are shipped.
The package uses SHA-256 file digests, gzip compression and cpio payload, with
rich dependencies, for an RPM 4.14+ reader baseline rather than newer zstd or
large-file payload formats.

RPM header/buildtime reproducibility is **best effort**, not a byte-for-byte
guarantee across RPM versions, vendor macro configurations or build platforms.
Use the same controlled build environment when comparing binary checksums.
