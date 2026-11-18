"""Compact tray presentation policy. Navigation never modifies the connection."""
from dataclasses import dataclass, replace


WEBDAV_TIMEOUT_LIMIT = 3


class WebDAVTimeoutGrace:
    """Keep the last known connected state through isolated local probe timeouts."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.connected_mount = None
        self.timeouts = 0

    def update(self, snapshot, busy=False):
        if busy or snapshot.recovery_busy:
            self.reset()
            return snapshot
        mount = (snapshot.mount_point, snapshot.mount_type, snapshot.port,
                 snapshot.webdav.invocation, snapshot.webdav.main_pid,
                 snapshot.rclone.invocation, snapshot.rclone.main_pid)
        if snapshot.state == 'online':
            self.connected_mount = mount
            self.timeouts = 0
            return snapshot
        if snapshot.state != 'offline' or not snapshot.webdav_delayed:
            self.reset()
            return snapshot
        if mount == self.connected_mount:
            self.timeouts += 1
            if self.timeouts < WEBDAV_TIMEOUT_LIMIT:
                return replace(snapshot, state='online', title='Sync drive is mounted — checking WebDAV',
                               detail='The local server was slow to respond. Both services are running; '
                                      'the app will check again before reporting a problem.')
        self.reset()
        return replace(snapshot, title='Sync WebDAV is not responding',
                       detail='Both services are running and the drive is still mounted. '
                              'Check diagnostics before restarting the services.')


def connection_notice(previous_state, snapshot):
    if snapshot.state == 'auth' and previous_state != 'auth':
        return snapshot.title, snapshot.detail
    if snapshot.state in {'offline', 'working'} and previous_state == 'online':
        if snapshot.webdav_delayed:
            return snapshot.title, snapshot.detail
        return 'Sync drive disconnected', 'Open Sync WebDAV for status and recovery options.'
    return None


@dataclass(frozen=True)
class TrayState:
    summary: str
    action: str
    action_label: str
    action_enabled: bool
    open_enabled: bool
    quit_enabled: bool


def tray_state(snapshot, busy=False, can_cancel=False) -> TrayState:
    if busy:
        return TrayState('Working…', 'cancel' if can_cancel else 'none',
                         'Cancel operation' if can_cancel else 'Operation in progress…',
                         bool(can_cancel), False, False)
    if snapshot is None:
        return TrayState('Checking connection…', 'none', 'Checking connection…', False, False, True)
    if snapshot.state == 'online':
        return TrayState('Connected', 'disconnect', 'Disconnect', True, True, True)
    if snapshot.state == 'auth' and snapshot.configured:
        return TrayState('Sign-in required', 'restore', 'Sign in & restore…', True, False, True)
    if snapshot.state == 'working':
        return TrayState('Connecting…', 'none', 'Connecting…', False, False, True)
    if not snapshot.configured:
        return TrayState('Not configured', 'setup', 'Connect to Sync…', True, False, True)
    return TrayState('Disconnected', 'restart', 'Connect', True, False, True)


# Six stable entries; no maintenance, diagnostic or preference switches in tray.
TRAY_ENTRY_KEYS = ('summary', 'overview', 'open', 'connection', 'preferences', 'quit')
PAGES = ('overview', 'preferences', 'diagnostics', 'help')
