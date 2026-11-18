# Changelog

## 1.0.0 — Initial public release

### Setup and connection management

- Guided installation of Sync's WebDAV server and required Linux packages.
- Browser-based authentication and automatic local WebDAV credential configuration.
- Sync Folder, Vault, and scoped-folder support with configurable mount location
  and local WebDAV port.
- Per-user systemd services, automatic startup, and optional pre-login startup.
- Discovery and explicit adoption of existing documented Sync WebDAV setups.
- Connection recovery, server updates, safe disconnect, and confirmed setup removal.

### Desktop experience

- Compact tray menu with status, open-drive, and context-aware connection actions.
- Overview, Preferences, Diagnostics, and Help pages.
- Consistent green, amber, and red tray status icons.
- Nautilus bookmarks and account Auto logout guidance with security-settings links.
- Configurable, optional read-only keep-alive with timestamped activity history.

### Safety and diagnostics

- Localhost-only WebDAV access and private credential/configuration storage.
- Private daemon logging with credential masking in displayed diagnostics.
- Distinct session-expiry, session-rejection, and unknown SDK error reporting.
- Bounded activity evidence with session timing, read outcomes, and durations.
- Ownership-checked stale-mount cleanup with separate lazy-detach confirmation.
- Cancellable operations, serialized recovery, private backups, and setup rollback.

### Distribution

- Per-user source installation and one architecture-independent Debian package
  with Python 3.8+/GTK 3.16+ baseline and older PolicyKit package-name support.
- Reproducible source/package builds, automated verification and a release CI
  matrix that installs the same `.deb` on Ubuntu 20.04/22.04/24.04/26.04 and
  Debian 11/12/13 before publishing.
- MIT-licensed application; Sync's proprietary server is obtained from the vendor
  at setup time and is not redistributed.
