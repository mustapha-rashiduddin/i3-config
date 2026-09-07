#!/usr/bin/env python3
"""Top-of-screen i3bar: system status blocks only (disk/mem/load/vol/bat/time).

The workspace buttons live on the bottom bar (ws.py); this bar never touches
the i3 tree. Status is sampled on a slow cadence except the clock, which ticks
every second.
"""
import json
import subprocess
import sys
import time
from datetime import datetime


def run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def vol_block():
    v = run(["pamixer", "--get-volume"])
    muted = run(["pamixer", "--get-mute"]) == "true"
    if not v:
        return "♪: ?"
    return f"♪: muted ({v}%)" if muted else f"♪: {v}%"


def bat_block():
    try:
        with open("/sys/class/power_supply/BAT0/capacity") as f:
            cap = f.read().strip()
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


def main():
    slow_last = 0.0
    slow_blocks = [None] * len(SLOW_FUNCS)
    first = True

    print(json.dumps({"version": 1}), flush=True)
    print("[", flush=True)

    while True:
        now = time.time()
        if now - slow_last >= SLOW_INTERVAL:
            slow_blocks = [fn() for fn in SLOW_FUNCS]
            slow_last = now
        blocks = [{"full_text": b} for b in slow_blocks] + [{"full_text": time_block()}]
        line = json.dumps(blocks, ensure_ascii=False)
        if not first:
            line = "," + line
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        first = False
        time.sleep(1)


if __name__ == "__main__":
    main()