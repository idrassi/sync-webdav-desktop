# Sync WebDAV Desktop

**Independent, unofficial Linux desktop application for Sync.com WebDAV.**
Version 1.0.0 — initial public release. Not affiliated with or endorsed by Sync.com.

[GitHub repository](https://github.com/idrassi/sync-webdav-desktop) ·
[Report an issue](https://github.com/idrassi/sync-webdav-desktop/issues) ·
[MIT License](LICENSE)

Set up a Sync drive through a graphical application instead of manually installing
WebDAV, copying passwords, editing Rclone configuration, or writing systemd units.
The tray app also monitors the drive and restores a rejected browser login.

## Compact tray & main window

The tray contains only six entries: a short status line, **Open Sync WebDAV**,
**Open drive**, a context-aware **Connect / Disconnect / Sign in & restore**
action, **Preferences…**, and **Quit app**. During an operation the connection
action becomes **Cancel operation** when cancellation is supported. Quitting the
app leaves the drive mounted. No maintenance/removal controls clutter the tray.

The main window uses four pages:
- **Overview:** connection status, drive location and everyday actions.
- **Preferences:** connection configuration, application startup, keep-alive,
  server update and confirmed removal of setup.
- **Diagnostics:** detailed service state, recovery, private logging and history.
- **Help:** account Auto logout guidance, security link and About.

## User workflow

1. Install and launch **Sync WebDAV Desktop**.
2. Choose **Connect to Sync…** for first setup, or open **Preferences → Configure connection…**.
3. Select **Sync Folder**, **Vault**, or a specific folder, and an empty mount
   directory inside your home folder. Choose automatic startup and optionally
   pre-login startup.
4. Click **Review setup**, read the plan, then **Connect in browser**.
5. Approve administrator prompts if missing packages or FUSE/lingering require
   them. Sign in to your account in the browser opened by Sync.
6. The app automatically configures the generated local WebDAV credential, starts
   the services, tests Rclone, verifies the mount, and adds a Files bookmark.

**No account password is requested by the application.** Browser authentication
is delegated to Sync's official executable. A new generated **local WebDAV**
password is transferred from the CLI's private output pipe into Rclone without
manual copying, shell history, process-argument exposure, or GUI logging.

One account/profile and one active drive are managed at a time. Remove the
current automatic mount setup before switching profiles. Existing documented
setups are discovered read-only; changing them requires explicit adoption.

## Features

- Tray status every five seconds: green connected, amber connecting/recovering,
  red disconnected or authentication required. Immutable PNG updates and guarded
  delayed refreshes prevent stale theme-cache rendering; Refresh status also
  requests an explicit repaint without touching the services.
- Main status window, service startup state, local endpoint, exact mount check,
  and redacted current-boot service diagnostics.
- Connect/configure, restart, browser sign-in and restore, disconnect, server
  update, remove automatic mount setup, startup toggle, and help/about.
- User systemd services; localhost-only WebDAV access (`--allow-from none`).
- Sync Folder, Vault, scoped folder, mount location, profile and port options.
- Distribution dependency installation via administrator authorization
  (`pkexec`) for Debian/Ubuntu/Mint/Zorin, Fedora, and Arch-family systems.
- FUSE preflight and optional module loading; no automatic chmod or broad process
  killing. A port collision is reported rather than stopping unrelated programs.
- Private local configuration backups, setup rollback, cancelled subprocess
  cleanup, and serialized operations.
- Explicit session-expired / session-rejected / unknown-SDK diagnostics; the first
  failure is retained separately from subsequent retries. An unknown SDK error
  followed by the vendor's `revoked` label is not presented as proof of revocation.
- Private WebDAV daemon logging (`--log-file`, `UMask=0077`) for new setups. The
  **Diagnostics → Enable private failure logging…** adds a logging-only override to
  supported legacy guide services, effective on their next start; it never signs
  in or restarts them by itself.
- Stale FUSE mount detection and stop → ownership check → normal unmount → start
  recovery. Busy orphaned mounts require a separate lazy-detach confirmation.
  No force unmount, sudo unmount, process killing, or automatic mount cleanup.
- Source archive and Debian `Architecture: all` package; MIT-licensed application.
  The proprietary Sync server is **not redistributed**; it is downloaded at setup
  using the official HTTPS installer, which verifies its binary checksum.

## Account Auto logout and session expiry

Sync’s **Settings → Security → Auto logout** account policy can also affect
browser-authenticated WebDAV sessions. A confirmed deployment’s repeated
one-hour expiry was caused by this account setting, not by Linux mount inactivity.

Open [Sync account security](https://cp.sync.com/account/security) and review
**Auto logout**. Sync offers **Never**, **1 hour**, and **24 hours**. Choose the
policy appropriate for your needs; longer sessions increase exposure on shared
or unattended computers. The application does not read or change the account
setting. After changing it, use **Sign in & restore** to obtain a fresh session.

The **Help** page provides **Open Sync account security** and **Why does my
session expire?…**. Expiry notifications/status include this hint, and the status
window shows a clickable account-security link when an expired session is reported.
Setup and help also explain the policy. Other causes of session expiry are still
possible, so the app never assumes your current policy value or diagnoses every
authentication failure as auto-logout.

Keep-alive **cannot override this account policy**. If Auto logout caused the
expiry, keep-alive is not a fix and may be disabled to avoid unnecessary requests.
The app leaves your keep-alive preference unchanged. See the vendor’s
[Account security guide](https://help.sync.com/hc/en-us/articles/38275611260947-Account-security).

## Optional read-only keep-alive

Enabled by default, every **60 seconds**. Open **Preferences → Read-only keep-alive**
to disable it or set an interval from **30 to 3600 seconds**. The same
options appear in the setup dialog. Preferences are saved without restarting
services or requiring browser sign-in. Older settings files use these defaults.

A fresh Rclone client performs one shallow `lsf --dirs-only --max-depth=1` against
the configured local WebDAV remote. Reading through the FUSE folder itself could
be served from cache, so the keep-alive deliberately bypasses mount listing
caches. Directory-name output and errors are discarded; no file contents are
downloaded or changed. Sync receives authenticated directory/notification access.

Runs only while this desktop app is open and the expected drive is connected.
It pauses while disconnected or during recovery, cancels promptly when disabled,
and never overlaps or catches up missed reads after suspend. The status window
shows whether it is paused, running, or when the last read succeeded/failed.
A background keep-alive does not make the tray icon amber. No automatic sign-in
or service restart is performed on ping failure.

**Diagnostics → Session & keep-alive history…** shows timestamped start/result
records, read durations, successful/failed counts, last successful activity before
server exit, gaps between recorded successes, and prior recorded expiries. The
same evidence appears in the status window and `--status-json`. Records are stored
in `~/.config/sync-webdav-desktop/logs/keepalive-history.jsonl` (0600 in a 0700
directory), limited to 256 KiB plus one 256 KiB rotated file. Only timestamps,
opaque attempt IDs, outcomes, intervals and timing are written—not directory
names, file paths, account email, credentials or raw command output.

The app extracts only the saved session's `issuedAt` metadata to associate reads
with a sign-in; it does not export the token. Reads before history collection
begins cannot be reconstructed, so gaps are reported explicitly rather than
inferred as successes. An unfinished read start has no proven outcome. History-write failure is visible
but does not make a successful read fail or break the connection. The app records
observed session timing, not a confirmed configured timeout.

**Experimental mitigation, not a guaranteed fix:** activity might help a sliding
inactivity timeout. It cannot extend a fixed session expiry or undo revocation.
Keep-alive reads can themselves reveal that a session expired and trigger the
vendor server's normal shutdown, just like browsing. It adds periodic network/API
requests; disable it if unwanted. Confirm effectiveness over multiple sessions
before attributing the recurring 76–91 minute disconnects to inactivity.

## Install

### Debian / Ubuntu / Mint / Zorin

Distribute `dist/sync-webdav-desktop_1.0.0_all.deb`. Users can open it in their
package installer, then launch **Sync WebDAV Desktop** from the application menu.
The package installs system GTK/tray prerequisites; the GUI handles remaining
runtime requirements and configuration after user approval.

For terminal-based package installation:

```bash
sudo apt install ./sync-webdav-desktop_1.0.0_all.deb
```

### Source distribution / Fedora / Arch

Extract `dist/sync-webdav-desktop-1.0.0.tar.gz`. If GTK/tray prerequisites are
missing, run the confirmed bootstrap once, then install as the normal user:

```bash
/usr/bin/python3 scripts/bootstrap.py
/usr/bin/python3 install.py
~/.local/bin/sync-webdav-desktop
```

The installer copies the app to stable per-user storage, so the extracted source
folder can subsequently be moved or removed. Source installation requires Python
3.10+, GTK 3, PyGObject and Ayatana AppIndicator system packages. See
[distribution instructions](docs/DISTRIBUTION.md) for package details.

Supported server architectures: **x86_64 / ARM64**. Requires systemd, a graphical
Linux desktop, a browser, network access, and administrator access when system
requirements are missing. GNOME needs an AppIndicator extension; Ubuntu already
provides one. The main window remains accessible if the desktop lacks a tray.
KDE/Xfce support depends on their status-notifier host and installed GTK packages.

## Existing setups

Launching is read-only. The default guide services `sync-webdav.service` and
`sync-rclone.service` are discovered along with mount/profile/port/config paths.
The setup dialog requires approval to adopt them before rewriting. Their
configuration is backed up privately. Existing unrelated Rclone remotes are kept.

Finish file transfers and close files on the drive before restart, sign-in,
reconfiguration, update, disconnect, or removal. These actions temporarily
unmount it. **Open drive** is disabled unless the expected Rclone mount and both
services are ready, to prevent saving into an unmounted local folder.

Removing setup deletes only application-managed systemd unit files. It keeps
cloud files, the mount folder, vendor executable, saved Sync profile and local
credentials. App package/source uninstallation does not remove Sync services.
Lingering is not automatically disabled on removal because other user apps may
rely on it. Disabling the app's login toggle does not disable drive services.

## Privacy and failure recovery

Private daemon log: `~/.config/sync-webdav-desktop/logs/webdav.log` (0600 in a
0700 directory; vendor rotation is retained). Displayed diagnostics are bounded
and masked; raw daemon logs can contain file paths, so do not share them blindly.
Verbose request logging is not enabled. Legacy services without `--log-file`
discard daemon child output until logging is configured.

If Rclone exits while a Files window holds the mount busy, recovery first attempts
normal `fusermount3 -u`. If that fails because the exact user-owned Sync mount is
busy and no Rclone owner is running, the app asks separately before `-u -z` lazy
detachment. Close Files windows/file handles first. Declining leaves the stale
mount unchanged and services stopped. A changed/foreign mount, unknown service
state, or live Rclone owner causes cleanup to fail closed. This cannot refresh an
expired Sync session automatically; browser sign-in is still required.

Settings: `~/.config/sync-webdav-desktop/settings.json` (non-secret).
New Rclone config: `~/.config/sync-webdav-desktop/rclone-<profile>.conf` (0600).
Rclone password obscuring is **not encryption**; protect your local account.

The CLI output used for automatic credential handoff is never sent to GUI logs
or persisted wholesale. Account passwords, browser authorization URLs, cookies
and tokens are not stored by this app. Sync itself stores its sign-in profile.

A generated password cannot be re-displayed by Sync. To avoid losing one if
setup is interrupted after issuance, the app immediately saves a **temporary
plaintext local WebDAV password recovery record**, 0600 inside a 0700 directory:
`~/.config/sync-webdav-desktop/credential-recovery/`. It is automatically reused
on retry and deleted after successful connection. It contains **only the local
WebDAV password**, not the account password/session. Never share this directory
or private backups. On configuration rollback, newly issued credentials are
reconciled with the previous same-profile endpoint; account/profile switching
in-place is rejected.

If an existing local credential is lost, approve **Replace local WebDAV
credential** in setup. This requires browser sign-in and invalidates the previous
password for **all other WebDAV clients**, which must then be updated separately.
No silent reset is performed. Failed/cancelled sign-in recovery can leave services
stopped; retry **Sign in & restore**. Package installs and vendor downloads cannot
be completely undone; cancelling a package operation may require package-manager
repair. Configuration backups do not back up or revert Sync's cloud data.

Diagnostics mask common credential formats and emails, but may contain folder
names; inspect before sharing. Do not export credentials, backups, or CLI raw
sign-in output. No automated telemetry is added by this application; the vendor
server's own settings and terms are separate.

## Development and verification

Clone the repository to work on or build the application:

```bash
git clone https://github.com/idrassi/sync-webdav-desktop.git
cd sync-webdav-desktop
```

Run checks and build distributable archives:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 app.py --status-json  # read-only diagnostics
/usr/bin/python3 app.py --smoke-test  # window/tray/status; no recovery
/usr/bin/python3 app.py --tray-icon-test  # display-only amber/green/red sequence
/usr/bin/python3 packaging/build.py
/usr/bin/python3 packaging/check_distribution.py
```

Tests use mocked commands and temporary directories for onboarding, cancellation,
credentials, rollback and distribution checks. Live monitoring and GTK/tray startup
were verified on Ubuntu GNOME. Cross-distribution, ARM64, privileged-installation,
and reboot compatibility should be verified on each target environment using
[the acceptance checklist](docs/ACCEPTANCE_TESTS.md). Availability of a source or
Debian package is not a claim of validation on every Linux distribution.

## References

Implemented from the vendor's documented instructions:

- https://help.sync.com/hc/en-us/articles/51295382529043-WebDAV-Guide-for-Linux
- https://help.sync.com/hc/en-us/articles/56223914702483-Set-up-Sync-as-an-automatically-mounted-drive-on-Linux

MIT license covers this application's code only. Sync trademarks and proprietary
server remain the property of their owners; users obtain that server from Sync.
