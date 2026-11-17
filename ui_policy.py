"""Compact tray presentation policy. Navigation never modifies the connection."""
from dataclasses import dataclass


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
