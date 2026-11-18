# Code review disposition

Follow-up to the findings in `CODE_REVIEW.md` for version 1.0.1. The original
review is left unchanged.

## Addressed

- **Rollback and desktop files:** GTK bookmarks and tray autostart are no longer
  snapshotted or restored by setup. They are updated only after connection
  commits. Concurrent creation or editing during a failed setup survives, and
  an integration failure leaves the drive connected with a warning. Unit,
  settings and Rclone rollback remains in place.
- **Startup and status errors:** Settings are loaded separately from GTK
  initialization. Invalid settings identify the file and suggest backing it up
  before renaming it; permission errors identify the access problem.
  `--status-json` returns a structured, redacted error and exit code 1.
- **Monitoring:** Tick exceptions are logged without removing the GLib timer.
  Failed executor submissions do not leave refresh or keep-alive claims stuck.
  Keep-alive completion still requests a refresh when rendering fails.
- **Unused terminal recovery:** Removed `recovery.py`, its backend helpers and
  result protocol, and their packaging requirements. Browser recovery continues
  through `setup_engine.restore`; operation locking is retained.
- **Credential retention:** New issuance atomically replaces one current record
  per profile before removing old UUID-format records. Older-format records
  remain readable for migration, and other profiles are not pruned. Successful
  connection removes the profile's recovery records.
- **Repeated credential output:** Duplicate username/password blocks no longer
  trigger duplicate issuance callbacks. Each username is consumed with its
  password; an unpaired later password cannot overwrite the captured credential.
- **Tool paths and scopes:** Setup uses fixed systemctl/loginctl paths. Folder
  validation rejects empty interior path segments while preserving existing
  leading/trailing slash normalization.
- **Installer integrity:** Added an optional `installer_sha256` settings pin.
  A mismatched download is not executed; default behavior still relies on HTTPS
  and the vendor's binary verification. Curl does not load a user `.curlrc`.
- **History performance:** Cache decoded, validated history by content digest,
  not the formatted report. Private-file checks still run on every read, and
  session metadata, age and expiry calculations remain live.
- **Write durability and retry noise:** Source installation fsyncs temporary
  files before replacement. Session-expiry recording failures have a 60-second
  retry interval and warn once per expiry identifier.
- **Desktop quoting:** Shared the existing two-layer quoting implementation and
  added real `gio launch` round trips with spaces, quotes, backslashes, dollar
  signs, backticks and percent field-code characters. These tests uncovered a
  separate executable-path problem: GLib checks the executable before expanding
  percent field codes. Source-installed desktop entries now invoke the fixed
  `/usr/bin/python3`, with the installed script as the quoted argument. The shell
  wrapper remains available for command-line use.
- **Quota-dependent tests:** The two RPM failures did reproduce on a later run
  in the host's quota-limited `/tmp`. Mocked RPM tests now redirect scratch to
  their own fixture while still asserting that production requests `/tmp`.
  Added injected EDQUOT/ENOSPC checks; no packaging failures are blanket-skipped.

## Deliberately unchanged or deferred

- **Password-before-username parsing:** Keep the documented username-first
  credential block. Accepting an unpaired password retroactively risks treating
  an unrelated prompt as the generated local credential.
- **Recovery TTL:** Keep the latest issued password until connection succeeds;
  expiring the only recoverable copy could strand the user. Retention is bounded
  by replacement per profile rather than by time, and remains documented as
  plaintext in a private directory.
- **Monitoring cadence:** Keep the five-second health check and three-probe
  timeout grace. Adaptive/event-driven diagnostics need separate measurement
  and fault-latency tests; caching a whole report by mtime would freeze session
  age and other live evidence.
- **Port check:** The vendor cannot accept a pre-bound socket. The preliminary
  bind check remains advisory; daemon startup and mount verification still
  determine success.
- **Internationalization:** A separate product enhancement, not a correctness
  fix in this change.
- **Existing tray icon API and TextView redraw optimization:** Left intact.

## Verification

Offline unit tests, including actual GLib launcher parsing, pass. Distribution
checks cover reproducibility, portable source rebuilds, payload allowlists,
source install/upgrade/uninstall and desktop validation. On this host,
`TMPDIR=/dev/shm` avoids unrelated temporary-storage quota failures during those
checks. Runtime, test and packaging sources also parse with Python 3.8 grammar.

Current source and Debian artifacts are rebuilt under `dist/1.0.1/`, leaving old
artifacts untouched. Actual RPM building is unavailable here because `rpm` and
`rpmbuild` are not installed. Full graphical smoke testing is not performed;
monitor callbacks are tested using fake GTK bindings without changing live
services, credentials or mounts.
