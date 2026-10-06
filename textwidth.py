#!/usr/bin/env python3
"""Bar font metrics, measured the way i3bar measures them.

i3bar lays out the status command's blocks right-aligned, sizing each one from
the rendered text (predict_text_width -> Pango). Anything that wants to pin
content to the left of the bar -- the workspace buttons in the status frame --
has to pad the line out to exactly the bar's width, so it needs those widths.
This module measures text with the very same Pango calls i3bar makes, on the
bar's font, at the DPI i3 resolves (libi3/dpi.c: the Xft.dpi X resource, else
the screen's physical size), including the logical_px() scaling that every
fixed i3bar distance (separators, padding) gets on hi-dpi screens.
"""
import ctypes
import ctypes.util
import glob
import math
import re
import subprocess

import traywin

SEP_PX = 9      # i3bar's default separator_block_width, in logical px
SB_HOFF_PX = 4  # i3bar's gap between the statusline and the tray, logical px


def _load(names):
    """CDLL for each name, found through ldconfig or the Nix store."""
    for name in names:
        paths = []
        found = ctypes.util.find_library(name)
        if found:
            paths.append(found)
        for pattern in (f"/nix/store/*/lib/lib{name}.so*",
                        f"/usr/lib/**/lib{name}.so*"):
            paths += sorted(glob.glob(pattern, recursive=True), reverse=True)
        for path in paths:
            try:
                yield ctypes.CDLL(path)
                break
            except OSError:
                continue
        else:
            yield None


def _xft_dpi():
    try:
        out = subprocess.run(["xrdb", "-query"], capture_output=True, text=True,
                             timeout=2).stdout
    except Exception:
        return None
    hit = re.search(r"^\s*Xft\.dpi:\s*(\d+(?:\.\d+)?)", out, re.M | re.I)
    return int(round(float(hit.group(1)))) if hit else None


class Metrics:
    """Text widths and fixed bar distances for the bar's font."""

    def __init__(self, font="monospace 14"):
        cairo, pango, pangocairo, gobject = _load(
            ["cairo", "pango-1.0", "pangocairo-1.0", "gobject-2.0"])
        self._x11 = traywin.lib()
        self._cairo = cairo
        self._pango = pango
        self._pangocairo = pangocairo
        self._gobject = gobject
        self._cache = {}
        self.dpi = 96
        self._layout = None
        self._desc = None
        if None in (cairo, pango, pangocairo, gobject) or self._x11 is None:
            return
        self._bind()
        self._setup(font)

    @property
    def ok(self):
        return self._layout is not None

    def _bind(self):
        self._cairo.cairo_xcb_surface_create.restype = ctypes.c_void_p
        self._cairo.cairo_xcb_surface_create.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int]
        self._cairo.cairo_create.restype = ctypes.c_void_p
        self._cairo.cairo_create.argtypes = [ctypes.c_void_p]
        self._cairo.cairo_destroy.argtypes = [ctypes.c_void_p]
        self._cairo.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
        self._cairo.cairo_status.argtypes = [ctypes.c_void_p]
        self._pangocairo.pango_cairo_create_context.restype = ctypes.c_void_p
        self._pangocairo.pango_cairo_create_context.argtypes = [ctypes.c_void_p]
        self._pangocairo.pango_cairo_context_set_resolution.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self._pangocairo.pango_cairo_update_layout.argtypes = [ctypes.c_void_p,
                                                              ctypes.c_void_p]
        self._pango.pango_layout_new.restype = ctypes.c_void_p
        self._pango.pango_layout_new.argtypes = [ctypes.c_void_p]
        self._pango.pango_layout_set_font_description.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p]
        self._pango.pango_layout_set_text.argtypes = [ctypes.c_void_p,
                                                     ctypes.c_char_p,
                                                     ctypes.c_int]
        self._pango.pango_layout_get_pixel_size.argtypes = [ctypes.c_void_p,
                                                           ctypes.POINTER(ctypes.c_int),
                                                           ctypes.POINTER(ctypes.c_int)]
        self._pango.pango_font_description_from_string.restype = ctypes.c_void_p
        self._pango.pango_font_description_from_string.argtypes = [
            ctypes.c_char_p]
        self._gobject.g_object_unref.argtypes = [ctypes.c_void_p]

    def _setup(self, font):
        dpy = self._x11.XOpenDisplay(None)
        if not dpy:
            return
        self._x11.XDefaultScreen.restype = ctypes.c_int
        self._x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
        self._x11.XDisplayHeight.restype = ctypes.c_int
        self._x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._x11.XDisplayHeightMM.restype = ctypes.c_int
        self._x11.XDisplayHeightMM.argtypes = [ctypes.c_void_p, ctypes.c_int]
        screen = self._x11.XDefaultScreen(dpy)
        self.dpi = _xft_dpi() or self._screen_dpi(dpy, screen)

        attrs = traywin._XWindowAttributes()
        root = self._x11.XDefaultRootWindow(dpy)
        if not self._x11.XGetWindowAttributes(dpy, root, ctypes.byref(attrs)):
            return
        surface = self._cairo.cairo_xcb_surface_create(dpy, root, attrs.visual,
                                                      1, 1)
        cr = self._cairo.cairo_create(surface)
        ctx = self._pangocairo.pango_cairo_create_context(cr)
        self._pangocairo.pango_cairo_context_set_resolution(ctx, self.dpi,
                                                            self.dpi)
        self._layout = self._pango.pango_layout_new(ctx)
        self._desc = self._pango.pango_font_description_from_string(
            font.encode())
        self._pango.pango_layout_set_font_description(self._layout, self._desc)
        self._cr = cr
        self._surface = surface

    def _screen_dpi(self, dpy, screen):
        px = self._x11.XDisplayHeight(dpy, screen)
        mm = self._x11.XDisplayHeightMM(dpy, screen)
        return int(round(px * 25.4 / mm)) if mm else 96

    def logical_px(self, logical):
        """i3's logical_px(): unscaled below 120 dpi, scaled above."""
        if (self.dpi / 96.0) < 1.25:
            return logical
        return math.ceil((self.dpi / 96.0) * logical)

    def width(self, text):
        """Rendered width of `text` in pixels, or 0 when unmeasurable."""
        if not text or not self.ok:
            return 0
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        self._pango.pango_layout_set_text(self._layout,
                                          text.encode("utf-8", "replace"), -1)
        self._pangocairo.pango_cairo_update_layout(self._cr, self._layout)
        width = ctypes.c_int()
        height = ctypes.c_int()
        self._pango.pango_layout_get_pixel_size(self._layout,
                                                ctypes.byref(width),
                                                ctypes.byref(height))
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[text] = width.value
        return width.value

    def separator(self):
        return self.logical_px(SEP_PX)

    def statusline_tray_gap(self):
        return self.logical_px(SB_HOFF_PX)