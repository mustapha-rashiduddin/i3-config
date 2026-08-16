#!/usr/bin/env python3
import json
import select
import subprocess
import time
from datetime import datetime


def run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def screen_width():
    w = 0
    try:
        data = json.loads(run(["i3-msg", "-t", "get_workspaces"]))
        for ws in data:
            w = max(w, ws.get("rect", {}).get("width", 0))
    except Exception:
        pass
    return w


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


def render_blocks():
    width = screen_width()
    blocks = [
        {
            "full_text": "",
            "separator": False,
            "align": "left",
            "min_width": max(width - 600, 0),
        }
    ]
    for text in [disk_block(), mem_block(), load_block(), vol_block(),
                 bat_block(), time_block()]:
        blocks.append({"full_text": text})
    return blocks


def main():
    # i3bar consumes the header separately, then feeds the rest of the pipe to
    # a streaming JSON parser that rejects trailing garbage. It therefore
    # expects ONE continuous top-level array (the i3status style):
    #   {"version":1}
    #   [
    #   [blocks]
    #   ,[blocks]
    #   ...
    # Each inner array is one bar update. Emitting separate top-level arrays
    # makes the parser fail on the second one.
    print(json.dumps({"version": 1}), flush=True)
    print("[", flush=True)
    first = True
    last_render = 0.0
    while True:
        try:
            line = json.dumps(render_blocks())
            if first:
                print(line, flush=True)
                first = False
            else:
                print("," + line, flush=True)
            last_render = time.time()
            proc = subprocess.Popen(
                ["i3-msg", "-t", "subscribe", "-m", '["workspace","window"]'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            while proc.poll() is None:
                ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                if ready:
                    line = proc.stdout.readline()
                    if line and time.time() - last_render >= 0.2:
                        print("," + json.dumps(render_blocks()), flush=True)
                        last_render = time.time()
                elif time.time() - last_render >= 1.0:
                    print("," + json.dumps(render_blocks()), flush=True)
                    last_render = time.time()
            proc.wait()
        except Exception:
            pass
        time.sleep(1)


if __name__ == "__main__":
    main()
