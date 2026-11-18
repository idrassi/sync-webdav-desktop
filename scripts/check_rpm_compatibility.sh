#!/bin/sh
# Package validation only inside disposable CI Docker containers.
set -eu
[ -f /.dockerenv ] || { echo 'Refusing to run outside a disposable Docker container'; exit 1; }
cd "${COMPATIBILITY_SOURCE_DIR:-/workspace}"
export PYTHONDONTWRITEBYTECODE=1
. /etc/os-release

case "$ID" in
    fedora)
        dnf -y install python3 python3-gobject-base gtk3 libayatana-appindicator-gtk3 \
            desktop-file-utils rclone fuse3 util-linux kmod polkit xdg-utils \
            xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon shadow-utils
        ;;
    centos|rhel|rocky|almalinux)
        # Extra repositories are enabled ONLY in this disposable test container,
        # never by the desktop application's package/setup scripts.
        dnf -y install epel-release
        dnf --enablerepo=crb -y install python3 python3-gobject-base gtk3 libayatana-appindicator-gtk3 \
            desktop-file-utils rclone fuse3 util-linux kmod polkit xdg-utils \
            xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon shadow-utils
        ;;
    opensuse-tumbleweed|opensuse-leap)
        zypper --non-interactive refresh
        zypper --non-interactive install --no-recommends 'python3 >= 3.8' python3-gobject \
            typelib-1_0-Gtk-3_0 typelib-1_0-AyatanaAppIndicator3-0_1 \
            desktop-file-utils rclone fuse3 util-linux kmod polkit xdg-utils \
            xorg-x11-server-Xvfb xauth dbus-1 dbus-1-daemon shadow
        ;;
    *) echo "No RPM validation recipe for $ID"; exit 1 ;;
esac

set -- dist/sync-webdav-desktop-*-1.noarch.rpm
[ "$#" -eq 1 ] && [ -f "$1" ] || { echo 'Expected one universal noarch RPM'; exit 1; }
case "$ID" in
    opensuse-*) zypper --non-interactive --no-gpg-checks install --no-recommends "./$1" ;;
    centos|rhel|rocky|almalinux) dnf --enablerepo=crb -y install "./$1" ;;
    *) dnf -y install "./$1" ;;
esac

useradd --create-home --shell /bin/sh ci-test
chown -R ci-test:ci-test "$PWD"
runuser -u ci-test -- /usr/bin/python3 -m unittest discover -s tests -q
# Also exercise installed runtime modules outside the source checkout. Package
# builder/release tests stay source-only because they are not runtime payload.
runuser -u ci-test -- env TEST_SOURCE="$PWD" /usr/bin/python3 - <<'PY'
import os, pathlib, shutil, sys, tempfile, unittest
source = pathlib.Path(os.environ['TEST_SOURCE'])
with tempfile.TemporaryDirectory(prefix='installed-rpm-tests-') as directory:
    root = pathlib.Path(directory)
    tests = root / 'tests'
    tests.mkdir()
    for path in (source / 'tests').glob('test_*.py'):
        if path.name not in {'test_release_notes.py', 'test_rpm_packaging.py', 'test_compatibility.py'}:
            shutil.copyfile(path, tests / path.name)
    os.chdir(root)
    sys.path.insert(0, '/usr/share/sync-webdav-desktop')
    import backend
    assert pathlib.Path(backend.__file__).parent == pathlib.Path('/usr/share/sync-webdav-desktop')
    suite = unittest.defaultTestLoader.discover(str(tests))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful(): raise SystemExit(1)
PY
rpm -V sync-webdav-desktop
desktop-file-validate /usr/share/applications/io.github.vera.SyncWebDAVTray.desktop
/usr/bin/python3 -m compileall -q /usr/share/sync-webdav-desktop
runuser -u ci-test -- /usr/bin/sync-webdav-desktop --version

# Portable Xvfb setup (openSUSE need not provide Debian's xvfb-run script).
display_file=$(mktemp)
Xvfb -displayfd 1 -screen 0 1280x800x24 -nolisten tcp -ac > "$display_file" 2>/tmp/sync-ci-xvfb.log &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true; rm -f "$display_file"' 0 HUP INT TERM
count=0
while [ ! -s "$display_file" ]; do
    count=$((count + 1))
    kill -0 "$xvfb_pid" 2>/dev/null || { cat /tmp/sync-ci-xvfb.log; exit 1; }
    [ "$count" -lt 50 ] || { cat /tmp/sync-ci-xvfb.log; exit 1; }
    sleep 0.1
done
display=":$(cat "$display_file")"
runuser -u ci-test -- env DISPLAY="$display" NO_AT_BRIDGE=1 dbus-run-session -- \
    /usr/bin/sync-webdav-desktop --smoke-test
printf 'PASS: universal RPM installation/tests/GTK smoke on %s\n' "$PRETTY_NAME"
