# Distributing Sync WebDAV Desktop 1.0.0

**Sync WebDAV Desktop is an independent, unofficial application.** It is not
produced, affiliated with, or endorsed by Sync.com. The application and included
SVG/PNG artwork are MIT licensed; see `LICENSE`. Sync.com names identify the service
and do not imply ownership of its trademarks.

## Supported environment

- Linux with **Python 3.8 or newer**, systemd user services, GTK 3.16+, and Ayatana
  AppIndicator introspection libraries. Use `/usr/bin/python3` (the distribution's
  Python), not a virtual environment without system GI bindings.
- GNOME needs an enabled **AppIndicator/KStatusNotifierItem extension** (Ubuntu's
  AppIndicators extension is suitable). GTK being installed alone does not add
  a GNOME tray. KDE Plasma's System Tray and Xfce's StatusNotifier/AppIndicator
  panel plugin can also display the icon; GTK is still required on these desktops.
- The app package contains only Python and SVG/PNG files: the Debian package is
  `Architecture: all`, usable on **x86_64/amd64 and ARM64/aarch64**. The separately
  downloaded Sync vendor binary must support the host architecture. That vendor
  dependency, FUSE availability, and desktop integration must be tested on each
  target; an `all` package does not guarantee vendor support on every platform.
- A logged-in graphical desktop and PolicyKit authentication agent are needed
  for optional administrator actions. Headless sessions, non-systemd systems,
  and older distribution releases are not supported by this package.

The runtime setup may offer installation of missing `curl`, `rclone`, `fuse3`, or
`kmod` through the distro's **apt/dnf/pacman using pkexec**, after user approval.
`pkexec | policykit-1` is a required Debian dependency alternative; older releases
provide pkexec through policykit-1. The other runtime tools are recommendations.
PolicyKit asks for administrator authorization, **not a Sync account password**.
Account sign-in uses the normal browser opened by Sync's CLI with private local
output pipes; no terminal emulator or VTE package is required. Device-code /
headless sign-in is not provided by this graphical release. The one-time local
WebDAV credential is handed to private Rclone configuration by the app, not by
this installer. Packaging never prompts for an account password, logs in, reads
credentials, or generates drive configuration.

## Build from a clean checkout (no packaging dependencies)

The upstream repository is https://github.com/idrassi/sync-webdav-desktop.

```sh
git clone https://github.com/idrassi/sync-webdav-desktop.git
cd sync-webdav-desktop
/usr/bin/python3 packaging/build.py
```

This uses only Python's standard library and creates:

- `dist/sync-webdav-desktop-1.0.0.tar.gz`
- `dist/sync-webdav-desktop_1.0.0_all.deb`

It runs no application code, imports no GI, downloads nothing, invokes no package
manager, and makes no changes outside the output directory. It does not require
`dpkg-deb`, a Python wheel backend, git, or root. Use `--source-only` to skip the
Debian package or `--output-dir PATH` to choose another output directory.

Inputs are a sorted **allowlist**: public root application `*.py` files; `install.py`;
`icons/*.svg` and `icons/*.png`; license, README and optional `pyproject.toml`; Markdown docs;
packaging templates/helpers; source bootstrap scripts; and `tests/test_*.py`.
Application modules such as `settings.py`, `setup_engine.py`, `authentication.py`,
`gui_setup.py`, and `wait_ready.py` are discovered **at build time**. Build release
archives from the final source tree. Installation/build/bootstrap/test helpers
are not runtime modules.
No proprietary executables or arbitrary recursive directories are copied.

`.git`, hidden files (except the explicit `.gitignore` metadata), Python caches,
environments, `dist`, runtime/test data,
`.env` files, Rclone configs, keys, downloaded vendor tools, and files with
credential/secret/private names are excluded. Symlink inputs are rejected. Public
source files must never contain embedded credentials: no filename-based exclusion
can sanitize secret literals in code. New asset formats or subpackages require an
intentional allowlist update and tests before a release.

Archive member order, ownership (root:root), permissions, timestamps, gzip headers,
and Debian ar metadata are normalized. Rebuilding identical inputs with the same
Python/zlib environment produces identical bytes. The default timestamp is
2026-01-01 UTC; optionally specify `SOURCE_DATE_EPOCH` (an integer Unix timestamp)
for both builds. Local file mtimes and the checkout's absolute path do not affect
the archives. Changing included source files changes archive hashes.

## Source installation (per user, never sudo)

```sh
tar -xzf sync-webdav-desktop-1.0.0.tar.gz
cd sync-webdav-desktop-1.0.0
/usr/bin/python3 scripts/bootstrap.py --check  # read-only prerequisite report
/usr/bin/python3 scripts/bootstrap.py          # explicitly confirm any missing OS packages
/usr/bin/python3 install.py                    # normal desktop user, no sudo
```

The bootstrap prints the exact proposed package command and requires interactive
confirmation before invoking **sudo**. It never silently refreshes package indexes
or installs packages, never downloads Sync, and does not perform app/drive setup.
Denying confirmation or using `--check` makes no package changes. A supported
distro's repositories must provide Python >=3.8. The helper cannot make an old
release supported merely by installing its old `python3` package.

Package equivalents for manual prerequisite installation:

| Distribution | GI/desktop prerequisites | System/authorization tools |
| --- | --- | --- |
| Debian/Ubuntu | `python3`, `python3-gi`, `gir1.2-gtk-3.0`, `gir1.2-ayatanaappindicator3-0.1` | `desktop-file-utils`, `systemd`, `util-linux`, `xdg-utils`, `pkexec` (or `policykit-1` on older releases) |
| Fedora / RHEL 9+ family | `python3`, `python3-gobject-base`, `gtk3`, `libayatana-appindicator-gtk3` | `desktop-file-utils`, `systemd`, `util-linux`, `xdg-utils`, `polkit`; EPEL/CRB may be required |
| openSUSE Leap 16 / Tumbleweed | `python3` (virtual provider), `python3-gobject`, `typelib-1_0-Gtk-3_0`, `typelib-1_0-AyatanaAppIndicator3-0_1` | `desktop-file-utils`, `systemd`, `util-linux`, `xdg-utils`, `polkit` |
| Arch | `python`, `python-gobject`, `gtk3`, `libayatana-appindicator` | `desktop-file-utils`, `systemd`, `util-linux`, `xdg-utils`, `polkit` |

`install.py` **copies** runtime Python modules and icons into
`$XDG_DATA_HOME/sync-webdav-desktop` (default
`~/.local/share/sync-webdav-desktop`). It creates:

- `~/.local/bin/sync-webdav-desktop`: forwards arguments to the installed
  `app.py` using `/usr/bin/python3`, with shell-safe path quoting.
- `$XDG_DATA_HOME/applications/io.github.vera.SyncWebDAVTray.desktop`: the
  **Sync WebDAV Desktop** launcher, with Desktop Entry-safe absolute paths.
- `$XDG_DATA_HOME/icons/hicolor/scalable/apps/io.github.vera.SyncWebDAVTray.svg`.
- `$XDG_CONFIG_HOME/autostart/io.github.vera.SyncWebDAVTray.desktop` with `--tray`
  (default config home: `~/.config`).

The source folder may then be moved or removed. A stable installed `APP_DIR` also
lets user-service `ExecStartPre` refer to the installed `wait_ready.py`; do not move
the installed application directory. The installer does not create or rewrite
those drive services itself. Updates rerun `install.py` and remove only obsolete
files recorded in the app's private installation manifest, preserving unowned files.

Use `--no-autostart` to omit startup **and remove this app's old autostart entry**.
Only the tray starts at desktop login; this is separate from enabling drive user
services at boot. Launch by the application menu or the absolute wrapper path if
`~/.local/bin` is not on `PATH`. Close/quit the running app before upgrading to
avoid mixing old loaded modules and new helper files.

### Existing manual Sync WebDAV setups

The application uses **`io.github.vera.SyncWebDAVTray`** for its desktop identity,
single-instance behavior, icon, and autostart filename. Installing the application
does not alter existing Sync profiles, Rclone settings, services, mounts, or cloud
files. Adopt a manually configured drive through the explicit setup flow if you
want the application to manage its service configuration.

## Debian installation and removal

```sh
sudo apt install ./sync-webdav-desktop_1.0.0_all.deb
```

This is an explicit administrator action. Unlike `dpkg -i`, apt resolves the hard
dependencies: `python3 (>= 3.8)`, `python3-gi`, `gir1.2-gtk-3.0 (>= 3.16)`,
`gir1.2-ayatanaappindicator3-0.1`, `desktop-file-utils`, `systemd`, `util-linux`,
`xdg-utils`, and `pkexec | policykit-1`. Recommendations are `curl`, `rclone`, `fuse3`, and `kmod`.

The package installs under `/usr/share/sync-webdav-desktop`, with
`/usr/bin/sync-webdav-desktop`, the same desktop ID and hicolor SVG icon, and
license/distribution documentation under `/usr/share/doc/sync-webdav-desktop`.
Its only maintainer-script actions are best-effort desktop/icon cache refreshes.
They do not download vendor software, create autostart entries, enable services,
change FUSE/kernel settings, or inspect any user's home directory. Desktop-login
autostart is opt-in through the application for Debian users.

When switching from a source installation, first quit the app and run the source
installer's `--uninstall` so its higher-priority per-user launcher and optional
`~/.local/bin` wrapper do not shadow the system package. Existing drive services
are preserved; migrate any old `APP_DIR`/`wait_ready.py` references through the
app's explicit setup before deleting their source copy. Avoid keeping both
installation types active under the same desktop ID.

```sh
/usr/bin/python3 install.py --uninstall # source install: removes only app copy/integration
sudo apt remove sync-webdav-desktop    # Debian: removes system app, not user data
```

**Neither removal method stops/deletes Sync user services, profiles, credentials,
Rclone configuration, mounts, or cloud files.** Source uninstall leaves unowned
files in the app directory intact. Debian cannot remove opt-in per-user autostart
entries; disable app autostart before removal (or remove only the matching
`io.github.vera.SyncWebDAVTray.desktop` entry). Service cleanup or disabling the
drive is a separate deliberate user action. Do not uninstall while file transfers
are active or a drive service relies on the installed readiness helper.

## RPM installation and removal

One `sync-webdav-desktop-1.0.0-1.noarch.rpm` uses dependency alternatives for Fedora,
RHEL/CentOS-compatible and openSUSE package names. It installs the same application
paths as the Debian package. Use DNF or Zypper to resolve its dependencies:

```sh
sudo dnf install ./sync-webdav-desktop-1.0.0-1.noarch.rpm
# openSUSE:
sudo zypper install ./sync-webdav-desktop-1.0.0-1.noarch.rpm
```

Modern default Python 3.8+ is required; RHEL/CentOS 7/8 and openSUSE Leap 15 are
not supported by this generic package. Enterprise Linux dependencies can require
administrator-enabled EPEL/CRB; no application/RPM script adds repositories. Only
cache refreshes run in package lifecycle scripts. Removing the RPM does not stop
or delete user Sync services, profiles, credentials, mounts or cloud files.
Remove/reconfigure automatic setup before uninstalling its readiness helper.
See [RPM build and support details](RPM.md).

## Privilege boundaries and the vendor binary

Normal desktop use and source app installation do not need sudo. The source
bootstrap needs sudo/admin rights **only if missing distribution prerequisites
are explicitly accepted**. Optional GUI runtime package installation, loading the
FUSE module, and enabling **boot persistence with `loginctl enable-linger`** may
require administrator authorization via pkexec. Linger is optional and should be
explained/confirmed: it allows user services to run without a logged-in desktop.
No packaging script enables it. Do not run the GUI as root.

**Do not redistribute the proprietary Sync binary or the vendor installer in the
source tarball or Debian package.** Runtime setup obtains the vendor's installer
separately after consent; its availability, architecture support, behavior, and
license remain the vendor's responsibility. The MIT license covers this companion
application only, not Sync.com software, account service, or cloud content. Consult the vendor’s current Linux setup and
architecture guidance:

- https://help.sync.com/hc/en-us/articles/51295382529043-WebDAV-Guide-for-Linux
- https://help.sync.com/hc/en-us/articles/56223914702483-Set-up-Sync-as-an-automatically-mounted-drive-on-Linux

## Offline packaging verification (no live install)

```sh
/usr/bin/python3 packaging/check_distribution.py
```

The checker builds twice into a temporary directory **inside `dist/`**, compares
hashes, checks archive membership/metadata, adds disposable excluded-name fixtures
to a copied checkout, rebuilds an extracted source archive, and simulates the
source installer using a temporary fake home/XDG tree. It verifies uninstall
preserves unrelated data, tests paths with spaces/quotes/percent signs, and calls
`desktop-file-validate`, `dpkg-deb --info`, and `dpkg-deb --contents` when available.
External validation tools are checks only, not build dependencies. Missing tools
are reported as skipped; install them explicitly yourself for release validation.
No package install, host-home changes, sudo, pkexec, authentication, services,
network requests, or application launch occur. Run this check against the final
source tree before distributing a release.

For direct artifact inspection without installing:

```sh
desktop-file-validate packaging/io.github.vera.SyncWebDAVTray.desktop
dpkg-deb --info dist/sync-webdav-desktop_1.0.0_all.deb
dpkg-deb --contents dist/sync-webdav-desktop_1.0.0_all.deb
tar -tzf dist/sync-webdav-desktop-1.0.0.tar.gz
sha256sum dist/*.tar.gz dist/*.deb
```
