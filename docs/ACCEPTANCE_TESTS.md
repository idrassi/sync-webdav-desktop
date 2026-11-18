# Release acceptance checklist

Use this checklist to validate Sync WebDAV Desktop 1.0.1 on target environments.
Automated tests cover mocked setup and distribution checks; Ubuntu GNOME has
monitoring/GUI validation. Run the following on disposable Linux VMs with a test
Sync account. Do not use a production account or directories containing valuable
files for destructive cases.

## Target matrix

- Ubuntu 20.04, 22.04, 24.04, 26.04 amd64: one `.deb`, systemd desktop and tray host.
- Debian 11, 12, 13 amd64: the same `.deb`, PolicyKit agent and browser.
- Legacy OS security support and vendor server compatibility are separate checks.
- Container CI covers package/tests/GUI startup, not live FUSE/cloud authentication.
- Fedora amd64: noarch RPM, GTK/AppIndicator packages, GNOME extension.
- CentOS Stream 9/10 and RHEL 9+ desktops: noarch RPM, approved EPEL/CRB or
  CodeReady Builder repositories; actual RHEL subscription testing is separate.
- openSUSE Leap 16/Tumbleweed: the same RPM, Zypper resolution and tray host.
- Arch amd64: source bootstrap and GUI requirements (full pacman system upgrade).
- ARM64 systemd Linux: compatible vendor binary and FUSE support.
- KDE/Xfce: GTK dependencies and a status-notifier/AppIndicator panel host.

## Fresh installation

1. Install the package or source app as documented. Confirm the package
   contains no vendor binary, credentials or home-specific files.
2. Launch from the desktop menu. Confirm Connect is available and drive actions
   are unavailable before setup.
3. Select an empty `~/sync-drive`, Sync Folder, port 4918. Review warnings.
4. Accept package authorization if tools are missing. Decline it in a separate
   run: confirm failure is visible and no user service files are installed.
5. Sign in using the browser, with account 2FA if enabled. No account password
   should be requested by the app. No local WebDAV password copying is needed.
6. Confirm green tray status, both services active, `findmnt --mountpoint
   ~/sync-drive` identifies the expected Rclone remote, and Files opens cloud
   content. Check private configuration/recovery file permissions.
7. Upload/download a disposable file, verify content remotely, then delete only
   that test file. Confirm the drive remains available after closing the window.
8. Quit/relaunch the monitor: mount stays available. Reboot: automatically enabled
   services and tray startup work; optional lingering starts before login.

## Configuration and compatibility

- Vault and scoped folders expose only the selected content.
- Mount path with spaces, quotes, `$`, and `%` works with systemd and Files.
- Different unprivileged port works; a port collision reports an actionable
  error and never kills the unrelated listener.
- Nonempty mount directory and symlinked configuration/mount paths are rejected.
- Existing documented default setup is discovered without changes. Adoption
  requires approval, preserves unrelated Rclone remotes and creates backups.
- Profile switching is rejected until the current automatic setup is removed.
- App uninstall alone keeps services/data but may remove their readiness helper;
  remove/reconfigure automatic setup first, as explained in the documentation.

## Recovery and failures

- Revoke only the test account's saved WebDAV login. Tray becomes red and shows
  sign-in required. Sign in & restore completes entirely in browser/app.
- Cancel before/during browser sign-in. App remains responsive, owned child
  processes exit, and status explains the result.
- Force an error after credential issuance (e.g. unavailable FUSE). The 0600
  recovery record retains the one-time local password, no raw auth output is
  logged, and retry reuses it. Successful connection removes recovery records.
- With an existing local password but missing Rclone config, normal Connect does
  not silently reset it. Explicit replacement warns about other clients and
  requires browser authorization.
- Disconnect stops services, not cloud files. Restart remounts the drive.
- Update uses the official installer as the user and restores prior running
  state. Test network failure/cancellation; package operations may need repair.
- Removal deletes only marked managed units. External changes to units prevent
  automatic removal. Cloud files, profiles and mount directories survive.
- Setup failure rolls back local units/settings; inspect rollback failure
  messages. Verify that Files bookmark or tray autostart edits made during
  sign-in survive failed setup. Desktop integration is applied only after a
  successful connection; an integration failure does not disconnect the drive.
  Vendor session/account changes and installed packages are not transactionally
  reversible.

## Activity evidence

- While connected, confirm each scheduled ping has a timestamped `started` and
  `success`/`failed`/`cancelled` outcome linked to the saved sign-in timestamp.
- Verify no names, paths, email, tokens or raw command output occur in the JSONL
  evidence. Confirm file 0600/directory 0700 and bounded rotation.
- On expiry, compare last successful read to server exit and session issue time.
  Do not call a configured TTL confirmed from detected-failure timing alone.
- Verify new login separates sessions and preserves prior recorded expiries.
- Failed/history-unavailable outcomes do not count as successful renewal activity.

## Auto logout guidance

- Expired status/notification displays the account-policy hint, while other auth
  errors are not automatically blamed on Auto logout.
- Expired status offers a link to https://cp.sync.com/account/security; the Help
  page shortcut and explanatory help are available even while connected.
- Opening the page only launches the browser; no account settings are scraped
  or modified, services restarted, or automatic authentication triggered.
- Setup/help/history explain policy options, shared-computer security exposure,
  fresh sign-in after policy changes, and keep-alive’s inability to override it.
- No account or keep-alive preference changes occur solely from viewing guidance.

## Compact UI navigation

- Tray has six entries, including the short status line. Advanced controls are
  in the window, not nested into a long tray menu.
- Connected: Open drive works and connection action is Disconnect. New setup:
  action is Connect to Sync. Expired: Sign in & restore remains prominent.
- While both services stay active and the verified drive remains mounted, one or
  two consecutive local WebDAV probe timeouts leave the tray connected without
  a disconnect notification. Diagnostics still reports the timeout. A healthy
  response resets the count; three consecutive timeouts show a WebDAV warning.
- Stopped services, lost mounts, and sign-in failures bypass this timeout grace.
- Busy: Open drive/Quit unavailable and Cancel appears only when supported.
- Preferences opens its page; all four tabs can be navigated without changing
  settings, signing in, restarting or disconnecting.
- Startup/keep-alive persist only after interaction; unsaved interval does not
  change scheduling. Maintenance/removal still require confirmation.
- Small window sizes scroll gracefully; logs live on Diagnostics, not Overview.

## Release gate

Record distribution/version/architecture, desktop, vendor Sync version, Rclone
version, test results, and known issues. Publish SHA-256 checksums for artifacts.
Do not claim Fedora/Arch/ARM64/reboot/authentication validation until performed.
Consider package signing, automated CI, independent security review, and upstream
vendor compatibility testing before broader public distribution.
