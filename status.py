#!/usr/bin/env python3
"""Top-of-screen i3bar: system status blocks plus the loadout lock indicator.

The workspace buttons live on the bottom bar (ws.py); this bar only shows the
engaged-loadout state and system status. The LOCKED/UNLOCKED/N/A block is
event-driven, not polled: it is recomputed when i3 notices a `mark` change
(load/unload/heal all flip `loadout:` marks) and when the speed-dial database
changes on disk (`plant` rewrites its global_workspace row). Everything else is
sampled on a slow cadence except the clock, which ticks every second.
"""
import ctypes
import fcntl
import json
import os
import subprocess
from select import select
import sys
import time
from datetime import datetime
from pathlib import Path

LOADOUT_MARK = "loadout:"
SD_DB = Path.home() / "emacs-speed-dial" / "speed-dial.sqlite"
BAT_FLASH_COLOR = "#ff0000"


def run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _has_loadout_mark(node):
    if node.get("marks") and any(m.startswith(LOADOUT_MARK) for m in node["marks"]):
        return True
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        if _has_loadout_mark(child):
            return True
    return False


def planted_db_root():
    if not SD_DB.is_file():
        return None
    out = run(["sqlite3", str(SD_DB),
               "SELECT value FROM state WHERE key='global_workspace'"])
    if not out:
        return None
    return Path(out.splitlines()[0])


def load_state():
    """LOCKED when a window carries a loadout mark; UNLOCKED when the speed-dial
    plant points at a loadout file that is not engaged; N/A otherwise."""
    try:
        tree = json.loads(run(["i3-msg", "-t", "get_tree"]))
    except Exception:
        tree = {}
    if _has_loadout_mark(tree):
        return "LOCKED"
    root = planted_db_root()
    if root is None:
        return "N/A"
    if (root / "loadout").is_file() or (root / "loadout.toml").is_file():
        return "UNLOCKED"
    return "N/A"


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


def subscribe_events():
    try:
        proc = subprocess.Popen(
            ["i3-msg", "-t", "subscribe", "-m", '["window"]'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        fl = fcntl.fcntl(proc.stdout.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)
        return proc
    except Exception:
        return None


def watch_db():
    """inotify watch on the speed-dial sqlite file and its directory.

    plant rewrites the DB in place, which closes with IN_CLOSE_WRITE; a reload
    of the whole DB device (safe writes/replace) shows up on the directory.
    """
    try:
        libc = ctypes.CDLL("libc.so.6")
        fd = libc.inotify_init()
        db_path = os.path.abspath(SD_DB).encode()
        libc.inotify_add_watch(fd, db_path, 0x00000008)  # IN_CLOSE_WRITE
        dbdir = os.path.abspath(SD_DB.parent).encode()
        libc.inotify_add_watch(fd, dbdir, 0x00000008 | 0x00000100 | 0x00000200)
        return fd
    except Exception:
        return None


def main():
    slow_last = 0.0
    slow_blocks = [None] * len(SLOW_FUNCS)
    first = True
    state = load_state()

    print(json.dumps({"version": 1}), flush=True)
    print("[", flush=True)

    sub = subscribe_events()
    db_fd = watch_db()
    sub_buf = ""

    def emit():
        nonlocal first
        color = "#00ff00" if state == "LOCKED" else "#ff5252"
        state_block = {"full_text": state, "color": color}
        blocks = [state_block]
        on_second = int(time.time()) % 2
        cap = bat_percent()
        low_bat = cap is not None and cap < 15 and not bat_charging()
        avail = mem_available_mb()
        low_mem = avail is not None and avail <= 800
        for idx, block in enumerate(slow_blocks):
            item = {"full_text": block}
            flash = (
                (idx == SLOW_FUNCS.index(bat_block) and low_bat)
                or (idx == SLOW_FUNCS.index(mem_block) and low_mem)
            )
            if flash and on_second and block is not None:
                item["color"] = BAT_FLASH_COLOR
            blocks.append(item)
        blocks += [{"full_text": time_block()}]
        line = json.dumps(blocks, ensure_ascii=False)
        if not first:
            line = "," + line
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        first = False

    last_emit = 0.0
    while True:
        fds = []
        if sub is not None:
            fds.append(sub.stdout)
        if db_fd is not None:
            fds.append(db_fd)
        try:
            ready, _, _ = select(fds, [], [], 0.5)
        except (OSError, ValueError):
            ready = []
        now = time.time()
        trigger = False
        if sub is not None and sub.stdout in ready:
            try:
                sub_buf += os.read(sub.stdout.fileno(), 4096).decode(errors="replace")
            except (OSError, ValueError):
                pass
            while "\n" in sub_buf:
                line, sub_buf = sub_buf.split("\n", 1)
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                # i3 has no standalone "mark" event: add/clear of a mark comes
                # as a window event with change=="mark". load/unload/heal all
                # add or clear `loadout:` marks, so that one change recomputes
                # the label. Other window events (title, focus, move) do not.
                if ev.get("change") in ("mark", "unmark"):
                    trigger = True
        if db_fd is not None and db_fd in ready:
            try:
                os.read(db_fd, 4096)
            except OSError:
                pass
            trigger = True
        if sub is not None and sub.poll() is not None:
            sub = subscribe_events()
        if trigger:
            state = load_state()
        if now - slow_last >= SLOW_INTERVAL:
            slow_blocks = [fn() for fn in SLOW_FUNCS]
            slow_last = now
        if trigger or now - last_emit >= 1.0:
            emit()
            last_emit = now


if __name__ == "__main__":
    main()