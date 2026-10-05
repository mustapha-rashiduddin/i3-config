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

XA_CARDINAL = 6  # Xatom.h
_XEMBED_MAPPED = 1


class _X11Hint(ctypes.Structure):
    # POINTER(c_char), not c_char_p: ctypes copies (and NULLs) c_char_p fields,
    # and XGetClassHint's two strings are Xmalloc'd, so they must be XFree'd.
    _fields_ = [
        ("res_name", ctypes.POINTER(ctypes.c_char)),
        ("res_class", ctypes.POINTER(ctypes.c_char)),
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