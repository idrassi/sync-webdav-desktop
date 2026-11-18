#!/bin/sh
# Run ONLY inside a disposable CI/Docker container, not on a user's workstation.
# The same universal .deb is installed without a Sync account/server/FUSE mount.
set -eu
[ -f /.dockerenv ] || { echo 'Refusing to run outside a disposable Docker container'; exit 1; }

cd "${COMPATIBILITY_SOURCE_DIR:-/workspace}"
export DEBIAN_FRONTEND=noninteractive
# Transient mirror errors must fail validation, not skip dependency checks.
export APT_CONFIG=/tmp/sync-ci-apt.conf
printf 'Acquire::Retries "3";\nAcquire::http::Timeout "30";\n' > "$APT_CONFIG"
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates python3 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
    desktop-file-utils rclone fuse3 util-linux kmod xdg-utils xvfb xauth dbus-x11

# --no-install-recommends still resolves every declared dependency, including
# pkexec | policykit-1. This tests package-name differences on actual repositories.
set -- dist/sync-webdav-desktop_*_all.deb
[ "$#" -eq 1 ] && [ -f "$1" ] || { echo 'Expected exactly one package'; exit 1; }
apt-get install -y --no-install-recommends "./$1"

# Unit tests and installation do not use a real systemd session, credentials,
# kernel FUSE or cloud data. Use an ordinary user for application startup.
useradd --create-home --shell /bin/sh ci-test
chown -R ci-test:ci-test "$PWD"
runuser -u ci-test -- /usr/bin/python3 -m unittest discover -s tests -q
desktop-file-validate /usr/share/applications/io.github.vera.SyncWebDAVTray.desktop
/usr/bin/python3 - <<'PY'
import subprocess
# Confirm older distro Rclone supports our private stdin credential handoff.
result = subprocess.run(['rclone', 'obscure', '-'], input='ci-test-password\n',
                        capture_output=True, text=True, check=True)
assert result.stdout.strip() and 'ci-test-password' not in result.stdout
import tempfile
with tempfile.TemporaryDirectory() as directory:
    subprocess.run(['rclone', 'lsf', directory, '--max-depth=1', '--dirs-only',
                    '--log-level=ERROR', '--retries=1', '--low-level-retries=1',
                    '--contimeout=5s', '--timeout=15s', '--disable=ListR'],
                   stdout=subprocess.DEVNULL, check=True)
PY

runuser -u ci-test -- /usr/bin/sync-webdav-desktop --version
/usr/bin/python3 -m compileall -q /usr/share/sync-webdav-desktop
# Isolated X display and session bus: no host desktop, tray or login involved.
runuser -u ci-test -- xvfb-run -a dbus-run-session -- \
    /usr/bin/sync-webdav-desktop --smoke-test

. /etc/os-release
printf 'PASS: universal package installation/tests/GTK smoke on %s\n' "$PRETTY_NAME"
