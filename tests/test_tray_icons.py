"""Tray tests need no GTK, session bus, services or cloud authentication."""
import os
from pathlib import Path
import tempfile
import unittest

from tray_icons import TrayIconPresenter


class Indicator:
    def __init__(self): self.icons = []
    def set_icon_full(self, path, title): self.icons.append((Path(path), title, Path(path).read_bytes()))


class Scheduler:
    def __init__(self):
        self.sources = {}; self.cancelled = []; self.next = 0
    def add(self, delay, callback):
        self.next += 1; self.sources[self.next] = (delay, callback); return self.next
    def remove(self, source): self.cancelled.append(source)
    def execute_all(self):
        for source, (_, callback) in list(self.sources.items()):
            if source not in self.cancelled: callback()
        self.sources.clear()


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.assets = self.runtime / 'assets'; self.assets.mkdir()
        for key, name in TrayIconPresenter.ASSETS.items(): (self.assets / name).write_bytes(key.encode())
        self.indicator = Indicator(); self.scheduler = Scheduler()
        self.presenter = TrayIconPresenter(self.indicator, self.assets, self.runtime,
                                            self.scheduler.add, self.scheduler.remove)

    def tearDown(self):
        self.presenter.close()
        self.temp.cleanup()

    def test_states_have_distinct_absolute_immutable_identities(self):
        self.presenter.update('working', 'Working')
        self.presenter.update('online', 'Connected')
        paths = [entry[0] for entry in self.indicator.icons]
        self.assertTrue(all(p.is_absolute() and p.suffix == '.png' for p in paths))
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(paths[0].read_bytes(), b'working')
        self.assertEqual(paths[1].read_bytes(), b'online')
        self.assertEqual(paths[1].stat().st_mode & 0o777, 0o600)
        self.assertEqual(paths[1].parent.stat().st_mode & 0o777, 0o700)

    def test_polling_same_status_does_not_flood_host(self):
        self.presenter.update('online', 'Connected')
        for _ in range(20): self.assertFalse(self.presenter.update('online', 'Connected'))
        self.assertEqual(len(self.indicator.icons), 1)
        self.assertEqual(len(self.scheduler.sources), 2)

    def test_old_timer_cannot_overwrite_green_even_if_it_runs_after_cancel(self):
        self.presenter.update('working', 'Working')
        old = [callback for _, callback in self.scheduler.sources.values()]
        self.presenter.update('online', 'Connected')
        for callback in old: callback()  # Simulate a raced cancellation.
        self.assertEqual(self.indicator.icons[-1][2], b'online')
        self.scheduler.execute_all()
        self.assertTrue(all(entry[2] == b'online' for entry in self.indicator.icons[1:]))

    def test_settle_updates_use_new_paths_same_pixels(self):
        self.presenter.update('online', 'Connected')
        self.scheduler.execute_all()
        self.assertEqual(len(self.indicator.icons), 3)
        self.assertEqual(len({entry[0] for entry in self.indicator.icons}), 3)
        self.assertTrue(all(entry[2] == b'online' for entry in self.indicator.icons))

    def test_reconnect_force_reannounces_same_state(self):
        self.presenter.update('online', 'Connected')
        before = self.indicator.icons[-1][0]
        self.presenter.update('online', 'Connected', force=True)
        self.assertNotEqual(self.indicator.icons[-1][0], before)

    def test_error_and_disconnected_map_to_red(self):
        self.presenter.update('auth', 'Sign in')
        self.assertEqual(self.indicator.icons[-1][2], b'error')
        self.presenter.update('offline', 'Disconnected')
        self.assertEqual(self.indicator.icons[-1][2], b'error')

    def test_files_bounded_and_cleaned_on_quit(self):
        for i in range(30): self.presenter.update('online' if i % 2 else 'working', str(i))
        self.assertLessEqual(len(list(self.presenter.directory.iterdir())), self.presenter.MAX_FILES)
        path = self.presenter.directory
        self.presenter.close()
        self.assertFalse(path.exists())
        self.assertFalse(self.presenter.update('online', 'Connected'))


if __name__ == '__main__': unittest.main()
