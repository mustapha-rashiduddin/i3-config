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
        self.handlers = []
        self.init_threads = 0

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

    def XInitThreads(self):
        self.init_threads += 1
        return 1

    def XSetErrorHandler(self, handler):
        self.handlers.append(handler)
        return 0


class _Callable:
    """A ctypes function pointer takes a signature and is callable; so is this."""

    def __init__(self, fn=None):
        self.fn = fn

    def __call__(self, *args):
        return 0 if self.fn is None else self.fn(*args)


class BlankLib:
    """What lib() meets while loading: it binds signatures onto the functions --
    XInitThreads and XSetErrorHandler among them -- and then calls those two."""

    def __init__(self):
        self.handlers = []
        self.init_threads = 0
        self.XInitThreads = _Callable(self._init_threads)
        self.XSetErrorHandler = _Callable(self._set_error_handler)

    def __getattr__(self, _name):
        return _Callable()

    def _init_threads(self):
        self.init_threads += 1
        return 1

    def _set_error_handler(self, handler):
        self.handlers.append(handler)
        return 0


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


class ReservedWidthTests(unittest.TestCase):
    """ws.py asks for the tray's width on every status frame."""

    def setUp(self):
        self.lib = FakeLib([])
        self.bars = patch.object(traywin, "_bar_windows",
                                return_value=[]).start()
        patch.object(traywin, "_lib", self.lib).start()
        patch.object(traywin, "_cache", None).start()
        self.addCleanup(patch.stopall)

    def test_consecutive_frames_share_one_walk(self):
        traywin.reserved_width()
        traywin.reserved_width()

        self.assertEqual(self.bars.call_count, 1)

    def test_a_stale_cache_is_measured_again(self):
        traywin.reserved_width()

        traywin.reserved_width(ttl=0)

        self.assertEqual(self.bars.call_count, 2)

    def test_moving_the_icons_drops_the_cache(self):
        traywin.reserved_width()
        traywin.set_visible(True)
        traywin.reserved_width()

        self.assertEqual(self.bars.call_count, 2)

    def test_an_unmeasurable_tray_is_zero_and_not_cached(self):
        self.bars.side_effect = RuntimeError("no display")

        self.assertEqual(traywin.reserved_width(), 0)
        self.assertIsNone(traywin._cache)


class ErrorHandlerTests(unittest.TestCase):
    """libX11's own handler prints the protocol error and calls exit(1), which
    takes the bar down with it; a window dying between listing it and asking
    about it is ordinary here, so the handler has to be ours."""

    def load_lib(self):
        self.lib = BlankLib()
        stubs = [(patch.object(traywin.ctypes, "CDLL", return_value=self.lib)),
                 (patch.object(traywin.ctypes.util, "find_library",
                               return_value="libX11.so.6")),
                 (patch.object(traywin, "_lib", None))]
        for stub in stubs:
            stub.start()
            self.addCleanup(stub.stop)

    def test_loading_libx11_installs_the_swallowing_handler(self):
        self.load_lib()

        traywin.lib()

        self.assertEqual(self.lib.handlers, [traywin._ignore_error])
        self.assertEqual(self.lib.init_threads, 1)

    def test_the_handler_reports_the_error_as_handled(self):
        self.assertEqual(traywin._ignore_error(None, None), 0)


class NoLibTests(unittest.TestCase):
    def test_set_visible_without_libx11(self):
        with patch.object(traywin, "lib", return_value=None):
            self.assertFalse(traywin.set_visible(True))

    def test_reserved_width_without_libx11(self):
        with patch.object(traywin, "lib", return_value=None):
            self.assertEqual(traywin.reserved_width(), 0)


if __name__ == "__main__":
    unittest.main()