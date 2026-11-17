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

- Per-user source installation and architecture-independent Debian packaging.
- Reproducible source/package builds and automated verification.
- MIT-licensed application; Sync's proprietary server is obtained from the vendor
  at setup time and is not redistributed.
