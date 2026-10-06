#!/usr/bin/env python3
"""The one bar, at the bottom: workspace buttons behind a toggle, else system
status (lock, volume, battery, memory, disk, load, clock).

`toggle-ws.sh` writes SHOW_WS and pokes a tick, so the workspace row appears on
the next emit instead of waiting out the poll. The status blocks are sampled by
a background thread (every SLOW_INTERVAL, plus the lock block on mark/speed-dial
changes) so they never sit in the latency path of a workspace switch.
"""
import ctypes
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from select import select

import traywin
import textwidth

CHROME_CFG = os.path.expanduser("~/.config/google-chrome")
MARKER = os.path.expanduser("~/.config/i3/.chrome-launched")
MARKER_WINDOW = 6.0
OVERLAY = os.path.expanduser("~/.config/i3/window_names.json")
RENAME_PIPE = os.path.expanduser("~/.config/i3/.rename_pipe")
SHOW_WS = os.path.expanduser("~/.config/i3/.show_ws")
SD_DB = os.path.expanduser("~/emacs-speed-dial/speed-dial.sqlite")
BAT_FLASH_COLOR = "#ff0000"
LOCK_COLOR = "#00ff00"
UNLOCK_COLOR = "#ff5252"
_prof_cache = {}
_win_profiles = {}
_name_overlay = {}
_overlay_mtime = 0.0

ROW_KEYS = ["j", "k", "l", ";", "m", ",", ".", "/"]
LOADOUT_MARK = "loadout:"

CHAR_W = 12
PAD_W = 11
ELL = "\u2026"

BOX_WIDTH = 231

_snapshot = {
    "workspaces": [],
    "apps": {},
    "loadout_keys": set(),
    "screen_w": 0,
}
_refresh_event = threading.Event()
_refresh_pipe_r, _refresh_pipe_w = os.pipe()
_status_wake = threading.Event()
_status_lock = ("N/A", None)
_status_slow = []
_status_last_slow = 0.0
_last_focused = None
_focus_gen = 0
_cached_tree = None
_overlay_gen = 0
_show_ws = False
_tray_visible = True


def run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def get_workspaces():
    try:
        return json.loads(run(["i3-msg", "-t", "get_workspaces"]))
    except Exception:
        return []


def get_tree():
    try:
        return json.loads(run(["i3-msg", "-t", "get_tree"]))
    except Exception:
        return {}


def _short(label):
    return label.split("@", 1)[0]


def _profiles():
    key_files = [os.path.join(CHROME_CFG, "Local State")]
    mtime = 0.0
    try:
        mtime = max(os.path.getmtime(f) for f in key_files)
    except Exception:
        pass
    if _prof_cache.get("mtime") == mtime:
        return _prof_cache["map"]
    mapping = {}
    try:
        state = json.load(open(os.path.join(CHROME_CFG, "Local State")))
        dirs = (state.get("profile") or {}).get("info_cache") or {}
        for d in dirs:
            prefs = os.path.join(CHROME_CFG, d, "Preferences")
            email = None
            try:
                p = json.load(open(prefs))
                accs = p.get("account_info") or []
                if accs:
                    email = _short(accs[0].get("email") or "")
            except Exception:
                pass
            mapping[d] = email or _short(dirs[d].get("name") or "") or d
    except Exception:
        pass
    _prof_cache["mtime"] = mtime
    _prof_cache["map"] = mapping
    return mapping


def marker_directory():
    try:
        if time.time() - os.path.getmtime(MARKER) <= MARKER_WINDOW:
            with open(MARKER) as f:
                d = f.read().strip()
            return d or None
    except Exception:
        pass
    return None


def chrome_label(xid):
    if xid in _win_profiles:
        return _win_profiles[xid]
    label = window_profile(xid) or "Google-chrome"
    _win_profiles[xid] = label
    return label


def window_profile(xid):
    r = run(["xprop", "-id", str(xid), "_NET_WM_PID"])
    if "_NET_WM_PID" not in r:
        return None
    try:
        pid = int(r.split("=", 1)[1].strip())
    except Exception:
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="replace").replace("\0", " ")
    except Exception:
        return None
    m = re.search(r"--user-data-dir=.*?/(Default|Profile \d+)(?:\s|$)", cmd)
    if not m:
        m = re.search(r"--profile-directory=(Default|Profile \d+)(?:\s|$)", cmd)
    directory = m.group(1) if m else "Default"
    return _profiles().get(directory, "Google-chrome")


def load_overlay():
    global _name_overlay, _overlay_mtime
    try:
        mtime = os.path.getmtime(OVERLAY)
    except Exception:
        mtime = 0.0
    if mtime == _overlay_mtime:
        return
    _overlay_mtime = mtime
    try:
        with open(OVERLAY) as f:
            _name_overlay = json.load(f)
    except Exception:
        _name_overlay = {}


def save_overlay():
    try:
        with open(OVERLAY, "w") as f:
            json.dump(_name_overlay, f, indent=2)
    except Exception:
        pass


def _collect_all_ids(node, out):
    out.add(str(node.get("id", "")))
    xid = node.get("window")
    if xid:
        out.add(str(xid))
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        _collect_all_ids(child, out)


def _remove_container(node, con_id, xid):
    xid_str = str(xid) if xid is not None else ""
    for key in ("nodes", "floating_nodes"):
        children = node.get(key, [])
        for i, child in enumerate(children):
            child_id = str(child.get("id", ""))
            child_xid = str(child.get("window", ""))
            if (con_id and child_id == con_id) or (xid_str and child_xid == xid_str):
                del children[i]
                return True
            if _remove_container(child, con_id, xid):
                return True
    return False


def collect_windows(node):
    out = []
    con_id = str(node.get("id", ""))
    xid = node.get("window")
    xid_str = str(xid) if xid else ""
    if con_id in _name_overlay:
        out.append(_name_overlay[con_id])
    elif xid_str and xid_str in _name_overlay:
        out.append(_name_overlay[xid_str])
    else:
        props = node.get("window_properties") or {}
        if props.get("class") == "Google-chrome":
            out.append(chrome_label(node.get("window")))
        elif props.get("class") == "com.system76.CosmicTerm":
            out.append("te")
        elif props.get("class"):
            out.append(props["class"])
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        out.extend(collect_windows(child))
    return out


def walk_workspaces(node):
    found = []
    if node.get("type") == "workspace":
        found.append(node)
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found.extend(walk_workspaces(child))
    return found


def _walk_nodes(node):
    yield node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from _walk_nodes(child)


def _parse_loadout_slots(path):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return set()
    used = set()
    auto = 0

    def add(slot):
        nonlocal auto
        if not slot:
            while auto < len(ROW_KEYS) and ROW_KEYS[auto] in used:
                auto += 1
            if auto >= len(ROW_KEYS):
                return
            slot = ROW_KEYS[auto]
            auto += 1
        used.add(slot)

    for key, items in data.items():
        if key == "emacs":
            if isinstance(items, dict):
                add(items.get("slot"))
            continue
        if key in ("terminal", "browser"):
            for item in items or []:
                add(item.get("slot") if isinstance(item, dict) else None)
    return used


def loadout_roots(tree):
    """Directories of the loadouts currently engaged in i3, from their marks.

    The engaged loadout root lives in i3's RAM as the `loadout:<root>` marks
    carried by its windows; everything else reads UNLOCKED from disk.
    """
    roots = set()
    if tree:
        for node in _walk_nodes(tree):
            for m in node.get("marks") or []:
                if m.startswith(LOADOUT_MARK):
                    root = m[len(LOADOUT_MARK):]
                    if root:
                        roots.add(root)
    return roots


def loadout_keys(tree):
    """Workspace keys used by the engaged loadout(s), green in the bar."""
    keys = set()
    for root in sorted(loadout_roots(tree)):
        for name in ("loadout", "loadout.toml"):
            path = os.path.join(root, name)
            if os.path.isfile(path):
                keys |= _parse_loadout_slots(path)
                break
    return keys


def vol_block():
    v = run(["pamixer", "--get-volume"])
    muted = run(["pamixer", "--get-mute"]) == "true"
    if not v:
        return "♪: ?"
    return f"♪: muted ({v}%)" if muted else f"♪: {v}%"


def bat_percent():
    try:
        with open("/sys/class/power_supply/BAT0/capacity") as f:
            return int(f.read().strip())
    except Exception:
        return None


def bat_charging():
    try:
        with open("/sys/class/power_supply/BAT0/status") as f:
            return f.read().strip() != "Discharging"
    except Exception:
        return None


def bat_block():
    cap = bat_percent()
    if cap is None:
        return "BAT: ?"
    try:
        with open("/sys/class/power_supply/BAT0/status") as f:
            st = f.read().strip()
    except Exception:
        return "BAT: ?"
    if st == "Charging":
        icon = "⚡ CHR"
    elif st == "Full":
        icon = "█ FULL"
    else:
        icon = "🔋 BAT"
    return f"{icon} {cap}%"


def mem_block():
    out = run(["free", "-h"])
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) >= 7 and f[0].startswith("Mem"):
            return f"{f[2]} | {f[6]}"
    return "MEM: ?"


def mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


def disk_block():
    out = run(["df", "-P", "-h", "/"])
    for line in out.splitlines():
        f = line.split()
        if len(f) == 6 and f[0] != "Filesystem":
            return f[3]
    return "DISK: ?"


def load_block():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[0]
    except Exception:
        return "?"


def time_block():
    return datetime.now().strftime("{ %A } %d/%m/%Y %H:%M:%S")


SLOW_FUNCS = (vol_block, bat_block, mem_block, disk_block, load_block)
SLOW_INTERVAL = 5.0
_BAT_IDX = SLOW_FUNCS.index(bat_block)
_MEM_IDX = SLOW_FUNCS.index(mem_block)

# i3bar's stock workspace button colors (i3bar/src/xcb.c colors), so the status
# frame's buttons are indistinguishable from the built-in ones.
WS_FG = ("#ffffff", "#ffffff", "#888888")
WS_BG = ("#285577", "#900000", "#222222")
WS_FOCUSED, WS_URGENT, WS_IDLE = range(3)
WS_BUTTON_W = 22
BAR_FONT = "monospace 14"  # the config's `font pango:monospace 14`, stripped
_metrics = None


def metrics():
    global _metrics
    if _metrics is None:
        _metrics = textwidth.Metrics(BAR_FONT)
    return _metrics


def planted_db_root():
    if not os.path.isfile(SD_DB):
        return None
    out = run(["sqlite3", SD_DB,
               "SELECT value FROM state WHERE key='global_workspace'"])
    if not out:
        return None
    return out.splitlines()[0]


def lock_state(tree):
    """(state, root) — LOCKED with the engaged loadout's directory, else
    UNLOCKED with the planted loadout directory, else "N/A" without a root."""
    roots = loadout_roots(tree)
    if roots:
        return "LOCKED", sorted(roots)[0]
    root = planted_db_root()
    if root and (os.path.isfile(os.path.join(root, "loadout"))
                 or os.path.isfile(os.path.join(root, "loadout.toml"))):
        return "UNLOCKED", root
    return "N/A", None


def sample_status():
    """One slow pass: the polled blocks plus the lock block. Called once before
    the first emit so the bar never paints with half its status missing."""
    global _status_lock, _status_slow, _status_last_slow
    _status_slow = [fn() for fn in SLOW_FUNCS]
    _status_lock = lock_state(_cached_tree)
    _status_last_slow = time.time()


def _bg_status():
    global _status_lock
    last_slow = _status_last_slow
    while True:
        _status_wake.wait(timeout=max(SLOW_INTERVAL - (time.time() - last_slow), 0.0))
        forced = _status_wake.is_set()
        _status_wake.clear()
        now = time.time()
        if not forced and now - last_slow < SLOW_INTERVAL:
            continue
        try:
            if forced and now - last_slow < SLOW_INTERVAL:
                _status_lock = lock_state(_cached_tree)
            else:
                sample_status()
                last_slow = _status_last_slow
# re-assert the tray: a bar restart (config reload) or a new tray
            # client re-docks the icons with the mapped flag set.
            traywin.set_visible(not _show_ws)
            os.write(_refresh_pipe_w, b"\n")
        except Exception:
            pass


def show_ws():
    """The workspace row shows whenever the toggle file has any content."""
    try:
        return os.path.getsize(SHOW_WS) > 0
    except OSError:
        return False


def apply_tray():
    """The tray belongs to the bar, not to the frames it prints, so the
    workspace row has to hide the icons i3bar docked; see traywin.py."""
    global _tray_visible
    want = not _show_ws
    if want == _tray_visible:
        return
    if traywin.set_visible(want):
        _tray_visible = want


def workspace_buttons():
    """Stock i3 workspace buttons for the status frame.

    i3bar draws its own buttons for the whole bar (`workspace_buttons`), which
    would show them in the workspace frame too, where ws.py already draws its
    own boxes -- so the status frame draws its own set instead, clickable
    through the same ws.<key> click events. Like the built-in ones, one button
    per workspace that exists, no gaps between them.
    """
    ws_by_name = {ws.get("name"): ws for ws in _snapshot["workspaces"]}
    blocks = []
    for key in ROW_KEYS:
        ws = ws_by_name.get(key)
        if not ws:
            continue
        focused = ws.get("focused") or _last_focused == key
        state = WS_FOCUSED if focused else (
            WS_URGENT if ws.get("urgent") else WS_IDLE)
        blocks.append({
            "full_text": key,
            "name": f"ws.{key}",
            "instance": key,
            "align": "center",
            "separator": False,
            "separator_block_width": 0,
            "min_width": WS_BUTTON_W,
            "background": WS_BG[state],
            "color": WS_FG[state],
        })
    return blocks


def _block_width(block, m):
    """How wide i3bar draws a block: min_width wins over the text width."""
    return max(m.width(block.get("full_text") or ""),
               block.get("min_width") or 0)


def _block_sep(block, m):
    return block.get("separator_block_width", m.separator())


def filler(buttons, system, screen_w):
    """Pad the line out to the bar's width, between the buttons and the system
    blocks.

    i3bar right-aligns the status command's blocks (x_dest = rect.w - tray -
    gap - statusline_width), so a line narrower than the bar leaves the left
    edge empty. It counts a line as every block's width plus a separator block
    after all but the last, and it reserves the tray's width plus a gap, so the
    status blocks stay flush right while the buttons sit at x = 0. A block with
    empty text gets no width at all, hence the space.
    """
    m = metrics()
    if not m.ok or not screen_w:
        return None
    tray = traywin.reserved_width()
    available = screen_w - tray - (m.statusline_tray_gap() if tray else 0)
    used = sum(_block_width(b, m) + _block_sep(b, m) for b in buttons)
    used += m.separator()  # the separator after the filler itself
    for i, block in enumerate(system):
        used += _block_width(block, m)
        if i < len(system) - 1:
            used += _block_sep(block, m)
    return {
        "full_text": " ",
        "separator": False,
        "align": "left",
        "min_width": max(available - used, 0),
    }


def status_blocks():
    buttons = workspace_buttons()
    system = system_blocks()
    pad = filler(buttons, system, _snapshot.get("screen_w") or 0)
    return buttons + ([pad] if pad else []) + system


def system_blocks():
    state, root = _status_lock
    blocks = [{
        "full_text": f"{root} {state.lower()}" if root else "N/A",
        "color": LOCK_COLOR if state == "LOCKED" else UNLOCK_COLOR,
    }]
    on_second = int(time.time()) % 2
    cap = bat_percent()
    low_bat = cap is not None and cap < 15 and not bat_charging()
    avail = mem_available_mb()
    low_mem = avail is not None and avail <= 800
    for idx, block in enumerate(_status_slow):
        item = {"full_text": block if block is not None else ""}
        flash = ((idx == _BAT_IDX and low_bat) or (idx == _MEM_IDX and low_mem))
        if flash and on_second:
            item["color"] = BAT_FLASH_COLOR
        blocks.append(item)
    blocks.append({"full_text": time_block()})
    return blocks


def truncate(label, width):
    max_chars = (width - 2 * PAD_W) // CHAR_W
    if len(label) <= max_chars:
        return label
    return label[:max_chars - 1] + ELL


def _rebuild_apps():
    global _snapshot, _overlay_gen
    if _cached_tree is None:
        return
    apps = {}
    for node in walk_workspaces(_cached_tree):
        name = node.get("name", "")
        if name == "__i3_scratch":
            continue
        apps_list = []
        for a in collect_windows(node):
            if a not in apps_list:
                apps_list.append(a)
        apps[name] = apps_list
    _snapshot = {**_snapshot, "apps": apps, "loadout_keys": loadout_keys(_cached_tree)}
    _overlay_gen += 1


def _refresh():
    global _snapshot, _cached_tree
    gen = _focus_gen
    ogen = _overlay_gen
    workspaces = get_workspaces()
    tree = get_tree()
    load_overlay()
    if _overlay_gen != ogen:
        return
    apps = {}
    for node in walk_workspaces(tree):
        name = node.get("name", "")
        if name == "__i3_scratch":
            continue
        apps_list = []
        for a in collect_windows(node):
            if a not in apps_list:
                apps_list.append(a)
        apps[name] = apps_list
    screen_w = max((ws.get("rect", {}).get("width", 0) for ws in workspaces), default=0)
    pf = _last_focused
    if pf:
        for ws in workspaces:
            ws["focused"] = ws.get("name") == pf
    if _focus_gen != gen or _overlay_gen != ogen:
        return
    _cached_tree = tree
    _snapshot = {
        "workspaces": workspaces,
        "apps": apps,
        "loadout_keys": loadout_keys(tree),
        "screen_w": screen_w,
    }


def _bg_refresh():
    last_cleanup = 0.0
    while True:
        _refresh_event.wait(timeout=1.0)
        _refresh_event.clear()
        try:
            _refresh()
        except Exception:
            pass
        try:
            os.write(_refresh_pipe_w, b"\n")
        except Exception:
            pass
        now = time.time()
        if now - last_cleanup >= 30 and _name_overlay:
            try:
                tree = get_tree()
                if tree:
                    known = set()
                    _collect_all_ids(tree, known)
                    changed = False
                    for con_id in list(_name_overlay.keys()):
                        if con_id not in known:
                            del _name_overlay[con_id]
                            changed = True
                    if changed:
                        save_overlay()
            except Exception:
                pass
            last_cleanup = now


def render(row_keys):
    if not _show_ws:
        return status_blocks()
    snap = _snapshot
    workspaces = snap["workspaces"]
    ws_by_name = {ws.get("name"): ws for ws in workspaces}
    apps = snap["apps"]
    screen_w = snap["screen_w"]
    loadout_keys = snap.get("loadout_keys") or set()
    width = BOX_WIDTH
    blocks = []
    for key in row_keys:
        ws = ws_by_name.get(key)
        app_list = apps.get(key, [])
        label = f"{key}:{','.join(app_list)}" if app_list else key
        text = " " + truncate(label, width) + " "
        green = key in loadout_keys
        if ws:
            if ws.get("focused"):
                bg, fg = ("#009900", "#ffffff") if green else ("#990000", "#ffffff")
            elif ws.get("urgent"):
                bg, fg = "#7a1010", "#ffffff"
            else:
                bg, fg = ("#2e2e2e", "#00ff00" if green else "#ff5252")
        elif _last_focused == key:
            bg, fg = ("#009900", "#ffffff") if green else ("#990000", "#ffffff")
        else:
            bg, fg = ("#1c1c1c", "#00ff00" if green else "#ff5252")
        blocks.append({
            "full_text": text,
            "name": f"ws.{key}",
            "instance": key,
            "align": "left",
            "separator": False,
            "min_width": width,
            "background": bg,
            "color": fg,
        })
    n = len(row_keys)
    boxes_width = n * width
    spacer_width = max(screen_w - boxes_width, 0) if screen_w else 0
    blocks.append({"full_text": "", "separator": False, "align": "left", "min_width": spacer_width})
    return blocks


def handle_event(line):
    global _cached_tree, _focus_gen, _overlay_gen, _show_ws
    try:
        ev = json.loads(line)
    except Exception:
        return
    change = ev.get("change")
    if change == "tick":
        # toggle-ws.sh pokes a tick so the workspace row flips on this emit
        # rather than on the next poll; the file is the state, the tick is
        # only the "look again now" nudge.
        _show_ws = show_ws()
        apply_tray()
        return
    if change == "focus" and ev.get("current", {}).get("type") == "workspace":
        new_name = ev["current"].get("name", "")
        if new_name:
            _last_focused = new_name
            _focus_gen += 1
            found = False
            for ws in _snapshot["workspaces"]:
                if ws.get("name") == new_name:
                    ws["focused"] = True
                    found = True
                else:
                    ws["focused"] = False
            if not found:
                _snapshot["workspaces"].append({"name": new_name, "focused": True})
        _refresh_event.set()
        return
    if change == "mark":
        # loadout lock/unlock and tag changes land here; refresh the tree
        # immediately instead of waiting for the 1s poll so workspace buttons
        # flip green/red the moment marks are added or cleared.
        _refresh_event.set()
        _status_wake.set()
        return
    c = ev.get("container") or {}
    xid = c.get("window")
    con_id = str(c.get("id", ""))
    if change == "close":
        _overlay_gen += 1
        if xid is not None:
            _win_profiles.pop(xid, None)
        overlay_changed = False
        if con_id in _name_overlay:
            del _name_overlay[con_id]
            overlay_changed = True
        if xid is not None:
            xid_str = str(xid)
            if xid_str in _name_overlay:
                del _name_overlay[xid_str]
                overlay_changed = True
        if overlay_changed:
            save_overlay()
        if _cached_tree is not None:
            _remove_container(_cached_tree, con_id, xid)
            _rebuild_apps()
        _refresh_event.set()
    elif change == "new":
        d = marker_directory()
        if d and xid is not None:
            _win_profiles[xid] = _profiles().get(d) or d
        _refresh_event.set()
    elif change == "move":
        try:
            _cached_tree = get_tree()
            c = ev.get("container") or {}
            moved_id = str(c.get("id", ""))
            if moved_id and _cached_tree:
                for node in walk_workspaces(_cached_tree):
                    name = node.get("name", "")
                    if name == "__i3_scratch":
                        continue
                    for child in node.get("nodes", []) + node.get("floating_nodes", []):
                        if str(child.get("id", "")) == moved_id:
                            if not any(ws.get("name") == name for ws in _snapshot["workspaces"]):
                                _snapshot["workspaces"].append({"name": name})
                            break
            _rebuild_apps()
        except Exception:
            pass
        _refresh_event.set()
    elif change == "title":
        _refresh_event.set()


def handle_click(line):
    try:
        ev = json.loads(line)
    except Exception:
        return
    name = ev.get("name", "")
    if not name.startswith("ws.") or ev.get("button") not in (1, 2, 3):
        return
    run(["i3-msg", "workspace", name[3:]])


def main():
    global _show_ws
    load_overlay()
    _refresh()
    _show_ws = show_ws()

    t = threading.Thread(target=_bg_refresh, daemon=True)
    t.start()
    sample_status()
    traywin.set_visible(not _show_ws)
    st = threading.Thread(target=_bg_status, daemon=True)
    st.start()

    first = True

    def emit():
        nonlocal first
        line = json.dumps(render(ROW_KEYS), ensure_ascii=False)
        if not first:
            line = "," + line
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        first = False

    print(json.dumps({"version": 1, "click_events": True}), flush=True)
    print("[", flush=True)
    emit()

    while True:
        try:
            proc = subprocess.Popen(
                ["i3-msg", "-t", "subscribe", "-m",
                 '["workspace","output","window","mark","tick"]'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except Exception:
            time.sleep(1)
            continue
        sub_buf = ""
        in_buf = ""
        refresh_fd = os.fdopen(_refresh_pipe_r, "rb", buffering=0, closefd=False)
        try:
            inotify_fd = None
            db_fd = None
            try:
                _libc = ctypes.CDLL("libc.so.6")
                _inotify_fd = _libc.inotify_init()
                _overlay_bytes = os.path.abspath(OVERLAY).encode()
                _libc.inotify_add_watch(_inotify_fd, _overlay_bytes,
                                        0x00000008 | 0x00000080)
                inotify_fd = _inotify_fd
            except Exception:
                pass
            try:
                # plant rewrites the speed-dial sqlite in place, which closes
                # with IN_CLOSE_WRITE; a whole-DB replace shows up on the dir.
                _db_inotify_fd = _libc.inotify_init()
                _libc.inotify_add_watch(_db_inotify_fd,
                                        os.path.abspath(SD_DB).encode(),
                                        0x00000008)
                _libc.inotify_add_watch(_db_inotify_fd,
                                        os.path.abspath(SD_DB).parent.encode(),
                                        0x00000008 | 0x00000100 | 0x00000200)
                db_fd = _db_inotify_fd
            except Exception:
                pass
            fds = [proc.stdout, sys.stdin, refresh_fd]
            if inotify_fd is not None:
                fds.append(inotify_fd)
            if db_fd is not None:
                fds.append(db_fd)
            rename_file = None
            try:
                rename_file = os.fdopen(os.open(RENAME_PIPE, os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0)
                fds.append(rename_file)
            except Exception:
                pass
            for fd in (proc.stdout.fileno(), sys.stdin.fileno(), _refresh_pipe_r):
                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            while proc.poll() is None:
                ready, _, _ = select(fds, [], [], 0.5)
                if proc.stdout in ready:
                    try:
                        sub_buf += os.read(proc.stdout.fileno(), 4096).decode(errors="replace")
                    except (OSError, ValueError):
                        pass
                    emitted = False
                    while "\n" in sub_buf:
                        line, sub_buf = sub_buf.split("\n", 1)
                        if line:
                            handle_event(line)
                            emitted = True
                    if emitted:
                        emit()
                if refresh_fd in ready:
                    try:
                        os.read(_refresh_pipe_r, 4096)
                    except OSError:
                        pass
                    emit()
                if inotify_fd is not None and inotify_fd in ready:
                    try:
                        os.read(inotify_fd, 4096)
                    except OSError:
                        pass
                    load_overlay()
                    _rebuild_apps()
                    emit()
                if db_fd is not None and db_fd in ready:
                    try:
                        os.read(db_fd, 4096)
                    except OSError:
                        pass
                    _status_wake.set()
                if sys.stdin in ready:
                    try:
                        in_buf += os.read(sys.stdin.fileno(), 4096).decode(errors="replace")
                    except (OSError, ValueError):
                        pass
                    while "\n" in in_buf:
                        line, in_buf = in_buf.split("\n", 1)
                        if line:
                            handle_click(line)
                            emit()
                if rename_file is not None and rename_file in ready:
                    try:
                        data = rename_file.read(4096).decode(errors="replace")
                    except (OSError, ValueError):
                        data = ""
                    try:
                        rename_file.close()
                    except Exception:
                        pass
                    try:
                        old = rename_file
                        rename_file = os.fdopen(os.open(RENAME_PIPE, os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0)
                        fds.remove(old)
                        fds.append(rename_file)
                    except Exception:
                        if rename_file in fds:
                            fds.remove(rename_file)
                        rename_file = None
                    for line in data.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        parts = line.split("\t", 1)
                        if len(parts) == 2:
                            xid_str, new_name = parts
                            global _name_overlay
                            _name_overlay[xid_str] = new_name
                            save_overlay()
                            _rebuild_apps()
                            emit()
                # the tick is the fast path; one stat per poll is the backstop
                if show_ws() != _show_ws:
                    _show_ws = show_ws()
                    apply_tray()
                    emit()
        except Exception:
            time.sleep(1)
        proc.wait()


if __name__ == "__main__":
    main()
