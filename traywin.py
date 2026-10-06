#!/usr/bin/env python3
"""Show/hide the system tray inside i3bar.

`tray_output` is a property of the bar, not of the frame the status command
prints, so i3bar keeps the tray clients (nm-applet, pnmixer) docked on every
frame of the one bottom bar -- including the workspace frames, where they must
not appear. The lever the tray protocol itself offers is the client's
`_XEMBED_INFO` mapped flag: i3bar watches that property and unmaps or maps the
client window to match (handle_property_notify() in i3bar/src/xcb.c). Mapping
or unmapping the windows behind i3bar's back does not work: GTK tray clients
treat an unmapped icon window as lost and destroy/re-create it, and a window
i3bar believes is unmapped does not stay mapped when mapped by hand.

The icons stay docked either way -- i3bar keeps reserving their width -- so
nothing has to be re-docked on the way back and the apps keep running.
"""
import ctypes
import ctypes.util
import glob

TRAY_LOFF_PX = 2  # i3bar's tray_loff_px, added on top of the icons' width

XA_CARDINAL = 6  # Xatom.h
_XEMBED_MAPPED = 1


class _X11Hint(ctypes.Structure):
    # POINTER(c_char), not c_char_p: ctypes copies (and NULLs) c_char_p fields,
    # and XGetClassHint's two strings are Xmalloc'd, so they must be XFree'd.
    _fields_ = [
        ("res_name", ctypes.POINTER(ctypes.c_char)),
        ("res_class", ctypes.POINTER(ctypes.c_char)),
    ]


class _XWindowAttributes(ctypes.Structure):
    # The whole struct, not just the fields read here: XGetWindowAttributes
    # writes every one of them, and a short struct is a heap corruption.
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


_lib = None


def lib():
    global _lib
    if _lib is not None:
        return _lib
    candidates = []
    found = ctypes.util.find_library("X11")
    if found:
        candidates.append(found)
    # NixOS keeps no ldconfig entry find_library can use.
    candidates += sorted(glob.glob("/nix/store/*-libx11-*/lib/libX11.so.6"),
                         reverse=True)
    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue
        ulong_p = ctypes.POINTER(ctypes.c_ulong)
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XQueryTree.restype = ctypes.c_int
        lib.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                   ulong_p, ulong_p,
                                   ctypes.POINTER(ulong_p),
                                   ctypes.POINTER(ctypes.c_uint)]
        lib.XGetClassHint.restype = ctypes.c_int
        lib.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                      ctypes.POINTER(_X11Hint)]
        lib.XGetWindowAttributes.restype = ctypes.c_int
        lib.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.POINTER(_XWindowAttributes)]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                    ctypes.c_int]
        lib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                        ctypes.c_ulong, ctypes.c_ulong,
                                        ctypes.c_int, ctypes.c_int,
                                        ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int]
        lib.XFree.argtypes = [ctypes.c_void_p]
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _lib = lib
        return _lib
    return None


def _children(dpy, window):
    root = ctypes.c_ulong()
    parent = ctypes.c_ulong()
    children = ctypes.POINTER(ctypes.c_ulong)()
    count = ctypes.c_uint()
    if not _lib.XQueryTree(dpy, window, ctypes.byref(root),
                           ctypes.byref(parent), ctypes.byref(children),
                           ctypes.byref(count)):
        return []
    try:
        return [children[i] for i in range(count.value)]
    finally:
        _lib.XFree(children)


def _klass(dpy, window):
    hint = _X11Hint()
    if not _lib.XGetClassHint(dpy, window, ctypes.byref(hint)):
        return None
    name, cls = hint.res_name, hint.res_class
    try:
        return ctypes.string_at(cls).decode(errors="replace") if cls else None
    finally:
        for ptr in (name, cls):
            if ptr:
                _lib.XFree(ptr)


def _bar_windows(dpy):
    """i3 reparents i3bar's window into one of its frames, so the bar is not a
    child of the root: walk the tree for it."""
    found = []

    def walk(window, depth):
        for child in _children(dpy, window):
            if _klass(dpy, child) == "i3bar":
                found.append(child)
            elif depth:
                walk(child, depth - 1)

    walk(_lib.XDefaultRootWindow(dpy), 3)
    return found


def tray_windows(dpy):
    """The tray clients i3bar has reparented into its bar window(s)."""
    out = []
    for bar in _bar_windows(dpy):
        out.extend(child for child in _children(dpy, bar)
                   if _klass(dpy, child) != "i3bar")
    return out


def _geometry(dpy, window):
    """(x, y, width, height, map_state) or None for a window that is gone."""
    attrs = _XWindowAttributes()
    if not _lib.XGetWindowAttributes(dpy, window, ctypes.byref(attrs)):
        return None
    return (attrs.x, attrs.y, attrs.width, attrs.height, attrs.map_state)


def reserved_width():
    """How much of the bar i3bar's tray takes up, as it lays it out.

    i3bar gives the statusline rect.w - tray_width - a small gap, and
    get_tray_width() is every mapped icon plus tray_padding each, plus
    tray_loff_px (2) once the tray is non-empty (i3bar/src/xcb.c). The icons
    themselves are laid out right to left, so the leftmost mapped icon's x is
    the tray's left edge minus that loff.
    """
    if lib() is None:
        return 0
    dpy = _lib.XOpenDisplay(None)
    if not dpy:
        return 0
    try:
        reserved = 0
        for bar in _bar_windows(dpy):
            bar_geom = _geometry(dpy, bar)
            if not bar_geom:
                continue
            left = None
            for client in _children(dpy, bar):
                geom = _geometry(dpy, client)
                if not geom or geom[4] != 2:  # IsViewable
                    continue
                left = geom[0] if left is None else min(left, geom[0])
            if left is not None:
                reserved = max(reserved, bar_geom[2] - left + TRAY_LOFF_PX)
        return reserved
    except Exception:
        return 0
    finally:
        _lib.XCloseDisplay(dpy)


def set_visible(visible):
    """Hide or show the docked tray icons by setting their _XEMBED_INFO flag."""
    if lib() is None:
        return False
    dpy = _lib.XOpenDisplay(None)
    if not dpy:
        return False
    try:
        prop = _lib.XInternAtom(dpy, b"_XEMBED_INFO", False)
        value = (ctypes.c_ulong * 2)(1, _XEMBED_MAPPED if visible else 0)
        windows = tray_windows(dpy)
        for window in windows:
            _lib.XChangeProperty(dpy, window, prop, XA_CARDINAL, 32,
                                 0, value, 2, 0)
        _lib.XSync(dpy, False)
        return bool(windows)
    except Exception:
        return False
    finally:
        _lib.XCloseDisplay(dpy)


if __name__ == "__main__":
    import sys
    hide = "--hide" in sys.argv
    print(f"tray {'hidden' if hide else 'shown'}: {set_visible(not hide)}")