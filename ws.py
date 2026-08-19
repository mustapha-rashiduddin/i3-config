#!/usr/bin/env python3
import ctypes
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from select import select

CHROME_CFG = os.path.expanduser("~/.config/google-chrome")
MARKER = os.path.expanduser("~/.config/i3/.chrome-launched")
MARKER_WINDOW = 6.0
OVERLAY = os.path.expanduser("~/.config/i3/window_names.json")
RENAME_PIPE = os.path.expanduser("~/.config/i3/.rename_pipe")
_prof_cache = {}
_win_profiles = {}
_name_overlay = {}
_overlay_mtime = 0.0

ROW_KEYS = ["j", "k", "l", ";", "m", ",", ".", "/"]
GREEN = set(ROW_KEYS[:4])

CHAR_W = 12
PAD_W = 11
ELL = "\u2026"

BOX_WIDTH = 130

_snapshot = {
    "workspaces": [],
    "apps": {},
    "sw": 0,
    "status": ["...", "...", "...", "...", "...", "..."],
    "screen_w": 0,
}
_refresh_event = threading.Event()
_refresh_pipe_r, _refresh_pipe_w = os.pipe()
_last_focused = None
_focus_gen = 0
_cached_tree = None
_overlay_gen = 0


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


def vol_block():
    v = run(["pamixer", "--get-volume"])
    muted = run(["pamixer", "--get-mute"]) == "true"
    if not v:
        return "?: ?"
    return f"?: muted ({v}%)" if muted else f"?: {v}%"


def bat_block():
    try:
        with open("/sys/class/power_supply/BAT0/capacity") as f:
            cap = f.read().strip()
        with open("/sys/class/power_supply/BAT0/status") as f:
            st = f.read().strip()
    except Exception:
        return "BAT: ?"
    if st == "Charging":
        icon = "CHR"
    elif st == "Full":
        icon = "FULL"
    else:
        icon = "BAT"
    return f"{icon} {cap}%"


def mem_block():
    out = run(["free", "-h"])
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) >= 7 and f[0].startswith("Mem"):
            return f"{f[2]} | {f[6]}"
    return "MEM: ?"


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
    _snapshot = {**_snapshot, "apps": apps}
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
    status_texts = [disk_block(), mem_block(), load_block(), vol_block(), bat_block(), time_block()]
    text_w = sum(2 * PAD_W + len(t) * CHAR_W for t in status_texts)
    tray_w = 6 * (2 * PAD_W + 3 * CHAR_W)
    sw = text_w + tray_w
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
        "sw": sw,
        "status": status_texts,
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
    snap = _snapshot
    workspaces = snap["workspaces"]
    ws_by_name = {ws.get("name"): ws for ws in workspaces}
    apps = snap["apps"]
    sw = snap["sw"]
    screen_w = snap["screen_w"]
    width = BOX_WIDTH
    blocks = []
    for key in row_keys:
        ws = ws_by_name.get(key)
        app_list = apps.get(key, [])
        label = f"{key}:{','.join(app_list)}" if app_list else key
        text = " " + truncate(label, width) + " "
        if ws:
            if ws.get("focused"):
                bg, fg = ("#009900", "#ffffff") if key in GREEN else ("#990000", "#ffffff")
            elif ws.get("urgent"):
                bg, fg = "#7a1010", "#ffffff"
            else:
                bg, fg = ("#2e2e2e", "#00ff00" if key in GREEN else "#ff5252")
        elif _last_focused == key:
            bg, fg = ("#009900", "#ffffff") if key in GREEN else ("#990000", "#ffffff")
        else:
            bg, fg = ("#1c1c1c", "#00ff00" if key in GREEN else "#ff5252")
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
    spacer_width = max(screen_w - boxes_width - sw, 0) if screen_w else 0
    blocks.append({"full_text": "", "separator": False, "align": "left", "min_width": spacer_width})
    for text in snap["status"]:
        blocks.append({"full_text": text})
    return blocks


def handle_event(line):
    global _cached_tree, _focus_gen, _overlay_gen
    try:
        ev = json.loads(line)
    except Exception:
        return
    change = ev.get("change")
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
    load_overlay()
    _refresh()

    t = threading.Thread(target=_bg_refresh, daemon=True)
    t.start()

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
                 '["workspace","output","window","tick"]'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except Exception:
            time.sleep(1)
            continue
        sub_buf = ""
        in_buf = ""
        refresh_fd = os.fdopen(_refresh_pipe_r, "rb", buffering=0, closefd=False)
        try:
            inotify_fd = None
            try:
                _libc = ctypes.CDLL("libc.so.6")
                _inotify_fd = _libc.inotify_init()
                _overlay_bytes = os.path.abspath(OVERLAY).encode()
                _libc.inotify_add_watch(_inotify_fd, _overlay_bytes,
                                        0x00000008 | 0x00000080)
                inotify_fd = _inotify_fd
            except Exception:
                pass
            fds = [proc.stdout, sys.stdin, refresh_fd]
            if inotify_fd is not None:
                fds.append(inotify_fd)
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
        except Exception:
            time.sleep(1)
        proc.wait()


if __name__ == "__main__":
    main()
