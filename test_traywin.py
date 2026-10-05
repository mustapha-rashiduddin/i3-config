import unittest
from unittest.mock import patch

import traywin


class FakeLib:
    """Records what traywin asks libX11 to do; no X server involved."""

    def __init__(self, windows):
        self.windows = windows
        self.changes = []
        self.opened = 0
        self.closed = 0
        self.atoms = {}

    def XOpenDisplay(self, _name):
        self.opened += 1
        return 1234

    def XCloseDisplay(self, dpy):
        self.closed += 1

    def XInternAtom(self, _dpy, name, _only_if_exists):
        self.atoms[name] = 42
        return 42

    def XChangeProperty(self, _dpy, window, prop, type_, format_, mode,
                        value, nelements, _tail):
        self.changes.append((window, prop, type_, format_, mode,
                             [v for v in value], nelements))

    def XSync(self, _dpy, _discard):
        pass


class SetVisibleTests(unittest.TestCase):
    def setUp(self):
        self.lib = FakeLib([0x100, 0xe00])
        self.wins_patcher = patch.object(traywin, "tray_windows",
                                         return_value=self.lib.windows)
        self.lib_patcher = patch.object(traywin, "_lib", self.lib)
        self.wins = self.wins_patcher.start()
        self.lib_patcher.start()
        self.addCleanup(self.wins_patcher.stop)
        self.addCleanup(self.lib_patcher.stop)

    def test_show_sets_the_mapped_flag_on_every_tray_window(self):
        self.assertTrue(traywin.set_visible(True))

        self.assertEqual([(c[0], c[5]) for c in self.lib.changes],
                         [(0x100, [1, 1]), (0xe00, [1, 1])])
        self.assertTrue(all(c[2] == traywin.XA_CARDINAL and c[3] == 32
                            for c in self.lib.changes))

    def test_hide_clears_the_mapped_flag(self):
        self.assertTrue(traywin.set_visible(False))

        self.assertEqual([(c[0], c[5]) for c in self.lib.changes],
                         [(0x100, [1, 0]), (0xe00, [1, 0])])

    def test_display_is_released_either_way(self):
        traywin.set_visible(True)

        self.assertEqual((self.lib.opened, self.lib.closed), (1, 1))

    def test_no_tray_windows_is_not_a_success(self):
        self.wins.return_value = []

        self.assertFalse(traywin.set_visible(True))
        self.assertEqual(self.lib.changes, [])


class NoLibTests(unittest.TestCase):
    def test_set_visible_without_libx11(self):
        with patch.object(traywin, "lib", return_value=None):
            self.assertFalse(traywin.set_visible(True))


if __name__ == "__main__":
    unittest.main()