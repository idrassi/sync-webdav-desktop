"""Race-resistant tray presentation, independent of the status/mount backend.

GNOME/AppIndicator hosts load icons asynchronously and may cache themed icons.
Publish immutable PNGs by absolute path, with generation-specific identities and
bounded delayed re-announcements. Never hide/re-register the indicator to refresh
it, and never allow an old timer to overwrite a newer status.
"""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable


class TrayIconPresenter:
    REFRESH_DELAYS_MS = (900, 2500)
    MAX_FILES = 24
    ASSETS = {'online': 'sync-webdav-online.png', 'working': 'sync-webdav-working.png',
              'error': 'sync-webdav-error.png'}

    def __init__(self, indicator, assets: Path, runtime: Path,
                 schedule: Callable, cancel: Callable):
        self.indicator = indicator
        self.assets = assets
        self.schedule = schedule
        self.cancel = cancel
        self.directory = Path(tempfile.mkdtemp(prefix='icons-', dir=runtime))
        # Rootless panel hosts share this user's runtime directory. Close old
        # presenters cleanly; the bounded immutable cache needs no polling IO.
        self.directory.chmod(0o700)
        self.generation = 0
        self.key = None
        self.title = ''
        self.pending = set()
        self.files = deque()
        self.closed = False

    def _publish(self, generation: int, phase: int) -> None:
        if self.closed or generation != self.generation:
            return
        source = self.assets / self.ASSETS[self.key]
        target = self.directory / f'{self.key}-{generation}-{phase}.png'
        # Immutable path/content: both shell cache and asynchronous loaders
        # observe a new identity, without a theme lookup or rewritten file.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(source.read_bytes())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        self.files.append(target)
        self.indicator.set_icon_full(str(target), self.title)
        while len(self.files) > self.MAX_FILES:
            self.files.popleft().unlink(missing_ok=True)

    def update(self, state: str, title: str, force: bool = False) -> bool:
        if self.closed:
            return False
        key = 'online' if state == 'online' else 'working' if state == 'working' else 'error'
        if (key, title) == (self.key, self.title) and not force:
            return False  # Polling must not flood the host's asynchronous loader.
        for source in self.pending:
            self.cancel(source)
        self.pending.clear()
        self.key, self.title = key, title
        self.generation += 1
        generation = self.generation
        self._publish(generation, 0)
        for phase, delay in enumerate(self.REFRESH_DELAYS_MS, 1):
            reference = []
            def settle(phase=phase, reference=reference):
                if reference:
                    self.pending.discard(reference[0])
                self._publish(generation, phase)
                return False
            reference.append(self.schedule(delay, settle))
            self.pending.add(reference[0])
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for source in self.pending:
            self.cancel(source)
        self.pending.clear()
        # The caller first makes the indicator passive, so the host releases it.
        shutil.rmtree(self.directory)
