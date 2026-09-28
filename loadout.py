#!/usr/bin/env python3
"""Project-local i3 loadouts.

The directory containing the TOML file is the loadout root.

Example loadout:

    [[terminal]]
    name = "sql"
    path = "./db"

    [[terminal]]
    name = "server"
    path = "."
    command = ["st", "-e", "mksh"]

    [[terminal]]
    name = "code"
    path = "./src"
    emacs = true  # spawn st -e emacs -nw at that path, planted on it, no `o` needed

    [[browser]]
    url = "https://developer.mozilla.org/en-US/docs/Web/CSS"
    scroll = "40%"   # open the page scrolled 40% down (or an integer pixel offset)
    name = "css"     # optional; defaults to the URL host when omitted

    [emacs]
    path = "."

Slots default to the first free row key in declaration order (j, k, l, ;,
m, ,, ., /), shared across terminals, browsers and emacs, so they can be
omitted above. An explicit `slot` still wins: it must be one of the row keys,
and the automatic scanner skips keys already taken by an explicit slot (or by
an earlier automatic assignment).

Run directly as:

    python3 loadout.py lock ./loadout
    python3 loadout.py unload
    python3 loadout.py chrome-theme dark

If this file is exposed on PATH as `lock`/`unload`, it also understands those
invocation names, so the intended shell UX is simply `load <dir>`.

`lock` materializes the loadout once: it adopts the first emacs, launches
missing entries, and drops confirmed leftover windows, then exits. No daemon
watches i3 afterwards — closing any window (terminals included) keeps it
closed. `unload` simply clears the loadout marks.

`heal` is triggered by the i3 keybindings that switch to a loadout workspace.
It finds the engaged loadout via the `loadout:<root>` marks left on the windows
(the root is kept in i3's RAM, never written to a file), re-reads the TOML, and
sweeps every entry: the managed window (its `loadout-win:<ident>` mark) must
exist, sit on its slot, show its assigned name, and be anchored on its loadout
directory (browser windows only need to exist on their slot — their content
lives in the page). There is no repair — the first discrepancy unloads the
loadout so every workspace button turns red; reload explicitly afterwards.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROW_KEYS = ("j", "k", "l", ";", "m", ",", ".", "/")
MARK_PREFIX = "loadout:"
WINDOW_MARK_PREFIX = "loadout-win:"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "i3-loadout"
STATE_FILE = CACHE_DIR / "state.json"
RENAME_PIPE = Path.home() / ".config/i3/.rename_pipe"
OVERLAY = Path.home() / ".config/i3/window_names.json"
BROWSER_BIN = "google-chrome-stable"
SCROLL_TIMEOUT = 10.0
DARK_READER_ID = "eimadpbcbfnmbkopoojfekhnkhdbieeh"
DARK_READER_TIMEOUT = 12.0


class LoadoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    ident: str
    kind: str
    slot: str
    name: str
    cwd: Path
    command: tuple[str, ...] = ()
    script: str | None = None
    emacs: bool = False
    url: str | None = None
    scroll: int | str | None = None

    @property
    def mark(self) -> str:
        return MARK_PREFIX + self.ident


@dataclass(frozen=True)
class Spec:
    source: Path
    root: Path
    entries: tuple[Entry, ...]

    @property
    def slots(self) -> frozenset[str]:
        return frozenset(e.slot for e in self.entries)


@dataclass(frozen=True)
class Window:
    con_id: int
    xid: int | None
    pid: int | None
    title: str
    workspace: str
    marks: tuple[str, ...]


def run(argv: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise LoadoutError(f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LoadoutError(f"command timed out: {' '.join(argv)}") from exc


def i3_request(*argv: str) -> str:
    result = run(["i3-msg", *argv])
    if result.returncode != 0:
        raise LoadoutError(result.stderr.strip() or "i3-msg failed")
    return result.stdout.strip()


def i3(command: str) -> str:
    return i3_request(command)


def quote(value: str) -> str:
    # i3 uses comma/semicolon as command separators, and two of our workspace
    # names are literally ',' and ';'. Always quote workspace/mark strings.
    return json.dumps(value)


def get_tree() -> dict[str, Any]:
    try:
        return json.loads(i3_request("-t", "get_tree"))
    except json.JSONDecodeError as exc:
        raise LoadoutError("i3 returned an invalid tree") from exc


def walk(node: dict[str, Any], workspace: str | None = None):
    if node.get("type") == "workspace":
        workspace = node.get("name") or workspace
    yield node, workspace
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from walk(child, workspace)


def window_from(node: dict[str, Any], workspace: str) -> Window:
    return Window(
        con_id=int(node["id"]),
        xid=int(node.get("window")),
        pid=int(node["pid"]) if node.get("pid") is not None else None,
        title=str(node.get("name") or ""),
        workspace=workspace,
        marks=tuple(node.get("marks") or ()),
    )


def window_class(node: dict[str, Any]) -> str:
    props = node.get("window_properties") or {}
    return str(props.get("class") or "")


def windows(tree: dict[str, Any]) -> list[Window]:
    out: list[Window] = []
    for node, workspace in walk(tree):
        xid = node.get("window")
        if xid is None or not workspace or workspace == "__i3_scratch":
            continue
        out.append(window_from(node, workspace))
    return out


def focused_workspace(tree: dict[str, Any]) -> str | None:
    for node, workspace in walk(tree):
        if workspace and node.get("focused"):
            return workspace
    return None


def focused_window(tree: dict[str, Any]) -> Window | None:
    for node, workspace in walk(tree):
        if (node.get("focused") and node.get("window") is not None
                and workspace and workspace != "__i3_scratch"):
            return window_from(node, workspace)
    return None


def resolve_cwd(root: Path, value: str) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_dir():
        raise LoadoutError(f"directory does not exist: {path}")
    return path


def load_spec(filename: str | os.PathLike[str]) -> Spec:
    source = Path(filename).expanduser().resolve()
    if not source.is_file():
        raise LoadoutError(f"loadout file does not exist: {source}")
    root = source.parent
    try:
        data = tomllib.loads(source.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LoadoutError(f"cannot read loadout: {exc}") from exc

    entries: list[Entry] = []
    used: set[str] = set()
    auto = 0

    def assign(slot: str | None, where: str) -> str:
        """Validate an explicit slot or hand out the first free row key."""
        nonlocal auto
        if slot is None:
            while auto < len(ROW_KEYS) and ROW_KEYS[auto] in used:
                auto += 1
            if auto >= len(ROW_KEYS):
                raise LoadoutError(f"{where}: no free slot left (ROW_KEYS exhausted)")
            slot = ROW_KEYS[auto]
            auto += 1
        elif slot not in ROW_KEYS:
            raise LoadoutError(f"{where}: invalid slot {slot!r}")
        used.add(slot)
        return slot

    def parse_browser(n: int, item: dict[str, Any]) -> Entry:
        slot = assign(item.get("slot"), f"browser {n}")
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://", "file://")):
            raise LoadoutError(f"browser {n}: url must be an http(s) or file URL")
        name = item.get("name") or urllib.parse.urlsplit(url).hostname or url
        path = item.get("path", ".")
        scroll = item.get("scroll")
        if scroll is not None:
            if isinstance(scroll, bool) or not isinstance(scroll, (int, str)):
                raise LoadoutError(
                    f"browser {n}: scroll must be an integer (pixels) or a percent string like \"55%\"")
            if isinstance(scroll, int) and scroll < 0:
                raise LoadoutError(f"browser {n}: scroll must be >= 0")
            if isinstance(scroll, str):
                m = re.fullmatch(r"(\d{1,3})%", scroll.strip())
                if m is None or int(m.group(1)) > 100:
                    raise LoadoutError(
                        f"browser {n}: scroll must be an integer (pixels) or a percent string like \"55%\"")
        if not isinstance(name, str) or not name.strip():
            raise LoadoutError(f"browser {n}: name is required")
        if not isinstance(path, str):
            raise LoadoutError(f"browser {n}: path must be a string")
        return Entry(
            ident=f"browser:{n}",
            kind="browser",
            slot=slot,
            name=name.strip(),
            cwd=resolve_cwd(root, path),
            url=url,
            scroll=scroll,
        )

    counts: dict[str, int] = {}
    for key, items in data.items():
        if key == "emacs":
            if not isinstance(items, dict):
                raise LoadoutError("[emacs] must be a table")
            slot = items.get("slot")
            path = items.get("path", ".")
            name = items.get("name", "emacs")
            slot = assign(slot, "emacs")
            if not isinstance(path, str):
                raise LoadoutError("emacs: path must be a string")
            if not isinstance(name, str) or not name.strip():
                raise LoadoutError("emacs: name must be a non-empty string")
            entries.append(Entry("emacs:0", "emacs", slot, name.strip(), resolve_cwd(root, path)))
            continue
        if key not in ("terminal", "browser"):
            continue
        if not isinstance(items, list):
            raise LoadoutError(f"[[{key}]] must be an array of tables")
        for item in items:
            n = counts.get(key, 0)
            counts[key] = n + 1
            if not isinstance(item, dict):
                raise LoadoutError(f"each [[{key}]] entry must be a table")
            if key == "browser":
                entries.append(parse_browser(n, item))
                continue
            slot = item.get("slot")
            name = item.get("name")
            path = item.get("path", ".")
            run_emacs = item.get("emacs", False)
            if not isinstance(run_emacs, bool):
                raise LoadoutError(f"terminal {n}: emacs must be a boolean")
            slot = assign(slot, f"terminal {n}")
            provided_command = "command" in item
            command = item.get("command", ["st", "-e", "mksh"])
            if run_emacs:
                if provided_command:
                    raise LoadoutError(f"terminal {n}: emacs and command are mutually exclusive")
                if "script" in item:
                    raise LoadoutError(f"terminal {n}: emacs and script are mutually exclusive")
                command = ["st", "-e", "emacs", "-nw"]
            if not isinstance(name, str) or not name.strip():
                raise LoadoutError(f"terminal {n}: name is required")
            if not isinstance(path, str):
                raise LoadoutError(f"terminal {n}: path must be a string")
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                raise LoadoutError(f"terminal {n}: command must be a non-empty array of strings")
            script = item.get("script")
            if script is not None and not isinstance(script, str):
                raise LoadoutError(f"terminal {n}: script must be a string")
            entries.append(Entry(
                ident=f"terminal:{n}",
                kind="terminal",
                slot=slot,
                name=name.strip(),
                cwd=resolve_cwd(root, path),
                command=tuple(command),
                script=script,
                emacs=run_emacs,
            ))

    if not entries:
        raise LoadoutError("loadout contains no [[terminal]], [[browser]] or [emacs] entries")
    return Spec(source, root, tuple(entries))


def occupied(spec: Spec, tree: dict[str, Any]) -> list[Window]:
    return [w for w in windows(tree) if w.workspace in spec.slots]


def plan(spec: Spec, tree: dict[str, Any]) -> tuple[dict[str, int], list[Window]]:
    """Sweep for the first emacs window and adopt it into the emacs entry.

    Returns the adopted map {entry.ident: con_id} (empty when the loadout has
    no [emacs] entry or no emacs is open) and the remaining windows that still
    occupy a target workspace and are not adopted (those are the only ones
    needing killing).
    """
    adopted: dict[str, int] = {}
    emacs_entry = next((e for e in spec.entries if e.kind == "emacs"), None)
    if emacs_entry is not None:
        for node, workspace in walk(tree):
            node_ws = workspace or ""
            if (window_class(node).lower() != "emacs" or not node_ws
                    or node_ws == "__i3_scratch" or node.get("window") is None):
                continue
            adopted[emacs_entry.ident] = window_from(node, node_ws).con_id
            break

    adopted_ids = set(adopted.values())
    leftover = [w for w in occupied(spec, tree) if w.con_id not in adopted_ids]
    return adopted, leftover


def active_spec() -> Spec | None:
    """Return the engaged loadout spec, located via the `loadout:<root>` i3 marks.

    The loadout root lives only in i3's tree (RAM), never in a file, so it stays
    engaged for as long as any of its windows exists.
    """
    roots: set[str] = set()
    for win in windows(get_tree()):
        for mark in win.marks:
            if mark.startswith(MARK_PREFIX):
                root = mark[len(MARK_PREFIX):]
                if root:
                    roots.add(root)
    for root in sorted(roots):
        source = Path(root) / "loadout"
        if not source.is_file():
            source = Path(root) / "loadout.toml"
        try:
            return load_spec(source)
        except LoadoutError:
            continue
    return None


def occupied_describe(found: list[Window]) -> str:
    """One line per occupied window slot, for the confirmation prompt."""
    if not found:
        return ""
    by_slot: dict[str, list[str]] = {}
    for win in found:
        by_slot.setdefault(win.workspace, []).append(win.title or str(win.xid))
    return "; ".join(f"{slot}: {', '.join(names)}" for slot, names in by_slot.items())


def prompt_replace(found: list[Window]) -> bool:
    by_slot: dict[str, list[str]] = {}
    for win in found:
        by_slot.setdefault(win.workspace, []).append(win.title or str(win.xid))
    print("Loadout target workspaces are occupied:")
    for slot in ROW_KEYS:
        if slot in by_slot:
            print(f"  {slot}: {', '.join(by_slot[slot])}")
    try:
        answer = input("Kill windows in those workspaces and load the loadout? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def clear_marks() -> None:
    try:
        all_windows = windows(get_tree())
    except LoadoutError:
        return
    for win in all_windows:
        for mark in win.marks:
            if mark.startswith(MARK_PREFIX) or mark.startswith(WINDOW_MARK_PREFIX):
                try:
                    i3(f"[con_id={win.con_id}] unmark {quote(mark)}")
                except LoadoutError:
                    pass


def rename_window(win: Window, name: str) -> None:
    if win.xid is None:
        return
    payload = f"{win.xid}\t{name}\n".encode()
    try:
        fd = os.open(RENAME_PIPE, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ENXIO, errno.EPIPE}:
            raise

    # ws.py is not listening on its FIFO: preserve the existing overlay format.
    try:
        overlay = json.loads(OVERLAY.read_text())
        if not isinstance(overlay, dict):
            overlay = {}
    except Exception:
        overlay = {}
    overlay[str(win.xid)] = name
    OVERLAY.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY.write_text(json.dumps(overlay, indent=2) + "\n")


def wait_new(before: set[int], *, pid: int | None = None, title: str | None = None,
             timeout: float = 12.0) -> Window:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = [w for w in windows(get_tree()) if w.con_id not in before]
        if title:
            match = [w for w in new if w.title == title]
            if len(match) == 1:
                return match[0]
        if pid is not None:
            match = [w for w in new if w.pid == pid]
            if len(match) == 1:
                return match[0]
        if title is None and len(new) == 1:
            return new[0]
        time.sleep(0.05)
    raise LoadoutError("timed out waiting for application window")


def wait_new_chrome(before: set[int], expected_class: str,
                    timeout: float = 25.0) -> Window:
    """Wait for the Google Chrome window this launch created.

    Each launch gets a unique WM_CLASS. Requiring Chrome's app-window role as
    well keeps a first-install extension help window from being claimed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for node, workspace in walk(get_tree()):
            if node.get("window") is None or not workspace or workspace == "__i3_scratch":
                continue
            if node["id"] in before:
                continue
            props = node.get("window_properties") or {}
            if (window_class(node) == expected_class
                    and props.get("window_role") == "pop-up"):
                return window_from(node, workspace)
        time.sleep(0.1)
    raise LoadoutError("timed out waiting for a browser window")


def browser_scroll_expr(scroll: int | str) -> str:
    """JS for CDP Runtime.evaluate: scroll the page to the target depth.

    A bare int is an absolute pixel offset; a string like "55%" is a fraction
    of the scrollable height, which is what you normally want for reading text.
    Returns JSON {top, want, max}: the scrollTop actually reached, the target
    (clamped to the page height), and the scrollable height — the caller
    retries until top == want on a non-empty page, so late-loading images that
    grow the page converge on the right fraction.
    """
    target = str(scroll) if isinstance(scroll, int) else f"Math.round(max * {float(scroll.strip().rstrip('%')) / 100.0:g})"
    return (
        "(() => {"
        "  const r = document.scrollingElement || document.documentElement;"
        "  const max = r.scrollHeight - r.clientHeight;"
        f"  r.scrollTop = {target};"
        f"  const want = Math.min({target}, max);"
        "  return JSON.stringify({top: r.scrollTop, want: want, max: max});"
        "})()"
    )


def ws_connect(url: str, timeout: float = 3.0) -> tuple[socket.socket, bytes]:
    """Upgrade a socket to a websocket client; returns (socket, leftover bytes)."""
    key = base64.b64encode(os.urandom(16)).decode()
    parts = urllib.parse.urlsplit(url)
    sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
    request = (
        f"GET {parts.path or '/'} HTTP/1.1\r\n"
        f"Host: {parts.hostname}:{parts.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(512)
        if not chunk:
            break
        data += chunk
    if b" 101 " not in data.split(b"\r\n", 1)[0]:
        raise ConnectionError("websocket handshake failed")
    return sock, data.split(b"\r\n\r\n", 1)[1]


def ws_send(sock: socket.socket, text: str) -> None:
    """Send a masked text frame (the only kind a browser expects from a client)."""
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header += bytes((0x80 | 126,)) + struct.pack(">H", n)
    else:
        header += bytes((0x80 | 127,)) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def ws_read_message(sock: socket.socket, buf: bytes) -> tuple[str, bytes]:
    """Read one complete message; returns (text, leftover bytes)."""
    while True:
        while len(buf) < 2:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        first, second = buf[0], buf[1]
        length = second & 0x7F
        idx = 2
        if length == 126:
            while len(buf) < 4:
                buf += sock.recv(4096)
            length = struct.unpack(">H", buf[2:4])[0]
            idx = 4
        elif length == 127:
            while len(buf) < 10:
                buf += sock.recv(4096)
            length = struct.unpack(">Q", buf[2:10])[0]
            idx = 10
        masked = bool(second & 0x80)
        mask = buf[idx:idx + 4] if masked else b""
        if masked:
            idx += 4
        while len(buf) < idx + length:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        payload = buf[idx:idx + length]
        buf = buf[idx + length:]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if first & 0x0F == 1 and first & 0x80:
            return payload.decode(errors="replace"), buf


def cdp_request(sock: socket.socket, buf: bytes, request_id: int, method: str,
                params: dict[str, Any] | None = None, *,
                timeout: float = 3.0) -> tuple[dict[str, Any], bytes]:
    """Send one CDP command and wait through unrelated events for its reply."""
    request: dict[str, Any] = {"id": request_id, "method": method, "params": params or {}}
    ws_send(sock, json.dumps(request))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        message, buf = ws_read_message(sock, buf)
        try:
            reply = json.loads(message)
        except json.JSONDecodeError:
            continue
        if reply.get("id") != request_id:
            continue
        error = reply.get("error")
        if isinstance(error, dict):
            raise LoadoutError(f"Chrome DevTools: {error.get('message', 'command failed')}")
        result = reply.get("result")
        return (result if isinstance(result, dict) else {}), buf
    raise LoadoutError(f"Chrome DevTools timed out running {method}")


def file_page_scroll_positions(port: str, *, include_web: bool = False
                               ) -> dict[str, tuple[float, float]]:
    """Capture page scroll before Dark Reader changes the document layout."""
    try:
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=1.5).read())
    except Exception as exc:
        raise LoadoutError("could not enumerate Chrome pages for Dark Reader") from exc
    positions: dict[str, tuple[float, float]] = {}
    for target in targets:
        target_id = target.get("id")
        url = target.get("url", "")
        allowed = url.startswith("file:") or (
            include_web and url.startswith(("http://", "https://")))
        if (target.get("type") != "page" or not allowed
                or not isinstance(target_id, str) or not target.get("webSocketDebuggerUrl")):
            continue
        sock: socket.socket | None = None
        try:
            sock, buf = ws_connect(target["webSocketDebuggerUrl"])
            result, _ = cdp_request(
                sock,
                buf,
                1,
                "Runtime.evaluate",
                {
                    "expression": "({x: window.scrollX, y: window.scrollY})",
                    "returnByValue": True,
                },
            )
            value = result.get("result", {}).get("value")
            if not isinstance(value, dict):
                raise LoadoutError("Chrome DevTools returned no scroll position")
            scroll_x = value.get("x")
            scroll_y = value.get("y")
            if not isinstance(scroll_x, (int, float)) or not isinstance(scroll_y, (int, float)):
                raise LoadoutError("Chrome DevTools returned an invalid scroll position")
            positions[target_id] = (scroll_x, scroll_y)
        except (OSError, ConnectionError, ValueError) as exc:
            raise LoadoutError(
                f"could not capture scroll position for {target.get('url') or 'local page'}"
            ) from exc
        finally:
            if sock is not None:
                sock.close()
    return positions


def reload_file_pages(port: str, enabled: bool | None = None,
                      positions: dict[str, tuple[float, float]] | None = None, *,
                      include_web: bool = False) -> None:
    """Reload app pages, preserve scroll, and verify local-file Dark Reader state."""
    try:
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=1.5).read())
    except Exception as exc:
        raise LoadoutError("could not enumerate Chrome pages for Dark Reader") from exc
    failures: list[str] = []
    for target in targets:
        url = target.get("url", "")
        is_file = url.startswith("file:")
        allowed = is_file or (include_web and url.startswith(("http://", "https://")))
        if (target.get("type") != "page" or not allowed
                or not target.get("webSocketDebuggerUrl")):
            continue
        sock: socket.socket | None = None
        request_id = 0
        try:
            sock, buf = ws_connect(target["webSocketDebuggerUrl"])
            position = (positions or {}).get(target.get("id"))
            if position is None:
                request_id += 1
                before, buf = cdp_request(
                    sock, buf, request_id, "Runtime.evaluate",
                    {
                        "expression": "({x: window.scrollX, y: window.scrollY})",
                        "returnByValue": True,
                    })
                value = before.get("result", {}).get("value")
                if not isinstance(value, dict):
                    value = {}
                scroll_x = value.get("x", 0)
                scroll_y = value.get("y", 0)
            else:
                scroll_x, scroll_y = position
            if not isinstance(scroll_x, (int, float)):
                scroll_x = 0
            if not isinstance(scroll_y, (int, float)):
                scroll_y = 0
            request_id += 1
            frame_tree, buf = cdp_request(
                sock,
                buf,
                request_id,
                "Page.getFrameTree",
            )
            old_loader = frame_tree.get("frameTree", {}).get("frame", {}).get("loaderId")
            if not isinstance(old_loader, str) or not old_loader:
                raise LoadoutError("Chrome DevTools returned no document loader")
            request_id += 1
            _, buf = cdp_request(sock, buf, request_id, "Page.reload")
            deadline = time.monotonic() + DARK_READER_TIMEOUT
            while time.monotonic() < deadline:
                request_id += 1
                try:
                    frame_tree, buf = cdp_request(
                        sock, buf, request_id, "Page.getFrameTree")
                    loader = frame_tree.get("frameTree", {}).get("frame", {}).get("loaderId")
                    if not loader or loader == old_loader:
                        time.sleep(0.1)
                        continue
                    request_id += 1
                    state, buf = cdp_request(
                        sock,
                        buf,
                        request_id,
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "({ready: document.readyState === 'complete', "
                                "active: document.documentElement.hasAttribute('data-darkreader-mode') || "
                                "document.querySelector('style.darkreader, link.darkreader') !== null})"
                            ),
                            "returnByValue": True,
                        },
                    )
                except LoadoutError:
                    time.sleep(0.1)
                    continue
                value = state.get("result", {}).get("value")
                expected = enabled if is_file else None
                if (isinstance(value, dict) and value.get("ready") is True
                        and (expected is None or value.get("active") is expected)):
                    request_id += 1
                    _, buf = cdp_request(
                        sock,
                        buf,
                        request_id,
                        "Runtime.evaluate",
                        {"expression": f"window.scrollTo({scroll_x:g}, {scroll_y:g})"},
                    )
                    break
                time.sleep(0.1)
            else:
                failures.append(target.get("url") or "local page")
        except (LoadoutError, OSError, ConnectionError, ValueError):
            failures.append(target.get("url") or "local page")
        finally:
            if sock is not None:
                sock.close()
    if failures:
        pages = ", ".join(failures)
        if enabled is None:
            raise LoadoutError(f"Dark Reader did not finish reloading {pages}")
        mode = "dark" if enabled else "light"
        raise LoadoutError(f"Dark Reader did not make {pages} {mode}")


def ensure_dark_reader_file_access(profile: Path, *, wait: bool = False,
                                   intended_url: str | None = None) -> bool:
    """Enable Dark Reader on file URLs through Chrome's extension settings API."""
    deadline = time.monotonic() + DARK_READER_TIMEOUT
    while True:
        try:
            lines = (profile / "DevToolsActivePort").read_text().splitlines()
        except OSError:
            lines = []
        if len(lines) >= 2 and lines[0].isdigit() and lines[1].startswith("/"):
            try:
                browser_sock, browser_buf = ws_connect(
                    f"ws://127.0.0.1:{lines[0]}{lines[1]}")
                break
            except (OSError, ConnectionError):
                pass
        if not wait or time.monotonic() >= deadline:
            return False
        time.sleep(0.1)

    target_id: str | None = None
    target_sock: socket.socket | None = None
    browser_request_id = 0

    def browser_command(method: str, params: dict[str, Any] | None = None,
                        *, timeout: float = 3.0) -> dict[str, Any]:
        nonlocal browser_buf, browser_request_id
        browser_request_id += 1
        result, browser_buf = cdp_request(
            browser_sock, browser_buf, browser_request_id, method, params, timeout=timeout)
        return result

    try:
        created = browser_command(
            "Target.createTarget", {"url": "chrome://extensions/", "background": True})
        target_id = created.get("targetId")
        if not isinstance(target_id, str):
            raise LoadoutError("Chrome DevTools did not create the extensions target")
        target_url = f"ws://127.0.0.1:{lines[0]}/devtools/page/{target_id}"
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                target_sock, target_buf = ws_connect(target_url)
                break
            except (OSError, ConnectionError):
                time.sleep(0.1)
        else:
            raise LoadoutError("Chrome DevTools could not attach to extension settings")

        expression = f"""
(async () => {{
    const id = {json.dumps(DARK_READER_ID)};
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const getInfo = () => new Promise((resolve, reject) => {{
        chrome.developerPrivate.getExtensionInfo(id, (info) => {{
            const error = chrome.runtime.lastError;
            error || !info ? reject(new Error(error?.message || "Dark Reader is not installed"))
                           : resolve(info);
        }});
    }});
    const update = () => new Promise((resolve, reject) => {{
        chrome.developerPrivate.updateExtensionConfiguration(
            {{extensionId: id, fileAccess: true}},
            () => {{
                const error = chrome.runtime.lastError;
                error ? reject(new Error(error.message)) : resolve();
            }}
        );
    }});
    const enable = () => new Promise((resolve, reject) => {{
        chrome.management.setEnabled(id, true, () => {{
            const error = chrome.runtime.lastError;
            error ? reject(new Error(error.message)) : resolve();
        }});
    }});

    let before = null;
    for (let i = 0; i < 80 && before === null; i++) {{
        try {{ before = await getInfo(); }} catch (_) {{ await sleep(100); }}
    }}
    if (before === null) {{ throw new Error("Dark Reader was not installed by policy"); }}
    let changed = before.state !== "ENABLED";
    if (changed) {{
        await enable();
        before = await getInfo();
    }}
    if (before.fileAccess?.isActive !== true) {{
        await update();
        changed = true;
    }}
    for (let i = 0; i < 80; i++) {{
        const after = await getInfo();
        if (after.state === "ENABLED" && after.fileAccess?.isEnabled === true &&
                after.fileAccess?.isActive === true) {{
            return {{changed}};
        }}
        await sleep(100);
    }}
    throw new Error("Chrome did not enable Dark Reader for file URLs");
}})()
""".strip()
        evaluated, target_buf = cdp_request(
            target_sock,
            target_buf,
            1,
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=DARK_READER_TIMEOUT,
        )
        if evaluated.get("exceptionDetails"):
            details = evaluated["exceptionDetails"]
            message = details.get("exception", {}).get("description") or details.get("text")
            raise LoadoutError(f"Dark Reader setup: {message or 'file access failed'}")
        value = evaluated.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise LoadoutError("Dark Reader did not confirm file access")
        changed = value.get("changed") is True
        if wait and changed:
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{lines[0]}/json", timeout=1.5).read())
            except Exception:
                targets = []
            wanted = urllib.parse.urldefrag(intended_url or "")[0].rstrip("/")
            for target in targets:
                url = target.get("url", "")
                help_target_id = target.get("id")
                actual = urllib.parse.urldefrag(url)[0].rstrip("/")
                if (target.get("type") != "page"
                        or not url.startswith("https://darkreader.org/help/")
                        or not isinstance(help_target_id, str)
                        or (wanted and (actual == wanted or actual.startswith(wanted + "/")))):
                    continue
                try:
                    browser_command("Target.closeTarget", {"targetId": help_target_id})
                except LoadoutError:
                    pass
    except (OSError, ConnectionError, ValueError) as exc:
        raise LoadoutError(f"Dark Reader setup failed for {profile.name}: {exc}") from exc
    finally:
        if target_id is not None:
            try:
                browser_command("Target.closeTarget", {"targetId": target_id})
            except (LoadoutError, OSError, ConnectionError):
                pass
        if target_sock is not None:
            target_sock.close()
        browser_sock.close()

    if changed:
        reload_file_pages(lines[0])
    return True


def dark_reader_expression(enabled: bool) -> str:
    """Persist Dark Reader settings from its existing service worker."""
    wanted = json.dumps(enabled)
    return f"""
(async () => {{
    const wanted = {wanted};
    const local = await chrome.storage.local.get(["syncSettings", "automation", "theme"]);
    const area = local.syncSettings === false ? chrome.storage.local : chrome.storage.sync;
    const before = await area.get(["automation", "theme"]);
    const automation = {{...(before.automation || {{}}), enabled: false}};
    const theme = {{...(before.theme || {{}}), mode: 1, engine: "dynamicTheme"}};
    await area.set({{enabled: wanted, automation, theme}});
    const stored = await area.get(["enabled", "automation", "theme"]);
    if (stored.enabled === wanted && stored.automation?.enabled === false &&
            stored.theme?.mode === 1 && stored.theme?.engine === "dynamicTheme") {{
        return {{
            enabled: stored.enabled,
            automationEnabled: stored.automation.enabled,
            mode: stored.theme.mode,
            engine: stored.theme.engine,
        }};
    }}
    throw new Error("Dark Reader state did not persist");
}})()
""".strip()


def set_dark_reader_theme(profile: Path, enabled: bool) -> bool:
    """Set Dark Reader in one live loadout Chrome profile; false means offline."""
    try:
        lines = (profile / "DevToolsActivePort").read_text().splitlines()
    except OSError:
        return False
    if len(lines) < 2 or not lines[0].isdigit() or not lines[1].startswith("/"):
        return False
    try:
        browser_probe, _ = ws_connect(f"ws://127.0.0.1:{lines[0]}{lines[1]}")
    except (OSError, ConnectionError):
        return False
    browser_probe.close()
    try:
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{lines[0]}/json", timeout=1.5).read())
    except Exception:
        return False
    pages = [
        target for target in targets
        if (target.get("type") == "page"
            and target.get("url", "").startswith(("file:", "http://", "https://"))
            and target.get("webSocketDebuggerUrl"))
    ]
    if not pages:
        return False
    positions = file_page_scroll_positions(lines[0], include_web=True)
    try:
        page_sock, page_buf = ws_connect(pages[0]["webSocketDebuggerUrl"])
        try:
            _, page_buf = cdp_request(page_sock, page_buf, 1, "ServiceWorker.enable")
            _, page_buf = cdp_request(
                page_sock,
                page_buf,
                2,
                "ServiceWorker.startWorker",
                {"scopeURL": f"chrome-extension://{DARK_READER_ID}/"},
            )
        finally:
            page_sock.close()

        deadline = time.monotonic() + 3.0
        worker = None
        while time.monotonic() < deadline:
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{lines[0]}/json", timeout=1.5).read())
                worker = next((target for target in targets
                               if target.get("type") == "service_worker"
                               and target.get("url", "").startswith(
                                   f"chrome-extension://{DARK_READER_ID}/")
                               and target.get("webSocketDebuggerUrl")), None)
                if worker is not None:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        if worker is None:
            raise LoadoutError(f"Dark Reader worker is not running in {profile.name}")

        worker_sock, worker_buf = ws_connect(worker["webSocketDebuggerUrl"])
        try:
            evaluated, worker_buf = cdp_request(
                worker_sock,
                worker_buf,
                1,
                "Runtime.evaluate",
                {
                    "expression": dark_reader_expression(enabled),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                timeout=6.0,
            )
        finally:
            worker_sock.close()
        if evaluated.get("exceptionDetails"):
            details = evaluated["exceptionDetails"]
            message = details.get("exception", {}).get("description") or details.get("text")
            raise LoadoutError(f"Dark Reader: {message or 'theme change failed'}")
        value = evaluated.get("result", {}).get("value")
        if (not isinstance(value, dict) or value.get("enabled") is not enabled
                or value.get("mode") != 1 or value.get("engine") != "dynamicTheme"):
            raise LoadoutError("Dark Reader did not persist the requested theme")

        browser_sock, browser_buf = ws_connect(f"ws://127.0.0.1:{lines[0]}{lines[1]}")
        try:
            stopped, browser_buf = cdp_request(
                browser_sock,
                browser_buf,
                1,
                "Target.closeTarget",
                {"targetId": worker["id"]},
            )
        finally:
            browser_sock.close()
        if stopped.get("success") is not True:
            raise LoadoutError("Chrome did not restart Dark Reader")
    except (OSError, ConnectionError, ValueError) as exc:
        raise LoadoutError(f"Dark Reader control failed for {profile.name}: {exc}") from exc

    reload_file_pages(
        lines[0], enabled, positions, include_web=True)
    return True


def chrome_theme(theme: str) -> int:
    """Set Dark Reader in every currently running isolated loadout profile."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with (CACHE_DIR / "chrome-theme.lock").open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        state = CACHE_DIR / "chrome-theme"
        temporary = CACHE_DIR / "chrome-theme.tmp"
        temporary.write_text(theme + "\n")
        os.replace(temporary, state)

        enabled = theme == "dark"
        failures: list[str] = []
        for profile in sorted(CACHE_DIR.glob("chrome-browser_*")):
            if not profile.is_dir():
                continue
            try:
                set_dark_reader_theme(profile, enabled)
            except LoadoutError as exc:
                failures.append(str(exc))
        if failures:
            raise LoadoutError("; ".join(failures))
    return 0


def desired_chrome_theme() -> str:
    """Return the last browser theme, falling back to the current terminal theme."""
    sources = (
        CACHE_DIR / "chrome-theme",
        Path.home() / ".config/config-manager/current-st-theme",
    )
    for source in sources:
        try:
            theme = source.read_text().strip()
        except OSError:
            continue
        if theme in {"light", "dark"}:
            return theme
    return "dark"


def apply_current_chrome_theme(profile: Path) -> None:
    """Apply the persisted mode to a browser that just joined the live profiles."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with (CACHE_DIR / "chrome-theme.lock").open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        theme = desired_chrome_theme()
        if not set_dark_reader_theme(profile, theme == "dark"):
            raise LoadoutError(f"could not apply Dark Reader theme to {profile.name}")


def chrome_scroll(target_ws: str, expression: str) -> dict[str, Any] | None:
    """Evaluate the scroll JS on the page and return its {top, want, max} JSON."""
    try:
        sock, buf = ws_connect(target_ws, timeout=3.0)
        try:
            sock.settimeout(3.0)
            ws_send(sock, json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                message, buf = ws_read_message(sock, buf)
                reply = json.loads(message)
                if reply.get("id") == 1:
                    value = reply.get("result", {}).get("result", {}).get("value")
                    if isinstance(value, str):
                        parsed = json.loads(value)
                        if isinstance(parsed, dict):
                            return parsed
                    return None
            return None
        finally:
            sock.close()
    except (OSError, ConnectionError, ValueError):
        return None


def scroll_site(profile: Path, entry: Entry) -> None:
    """Scroll the freshly opened app window to the entry's depth, if any.

    Chrome writes <profile>/DevToolsActivePort when started with
    --remote-debugging-port=0; the port gates the DevTools HTTP + websocket
    described by /json. This is best-effort: an unreachable page simply stays
    at the top.
    """
    expression = browser_scroll_expr(entry.scroll)
    if expression is None:
        return
    devtools = profile / "DevToolsActivePort"
    port: str | None = None
    deadline = time.monotonic() + SCROLL_TIMEOUT
    while time.monotonic() < deadline:
        if port is None:
            try:
                lines = devtools.read_text().splitlines()
                if lines and lines[0].isdigit():
                    port = lines[0]
            except OSError:
                pass
        if port is not None:
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json", timeout=1.5).read())
            except Exception:
                targets = []
            page = next((t for t in targets
                         if t.get("type") == "page" and t.get("url") == entry.url
                         and t.get("webSocketDebuggerUrl")), None)
            if page is None:
                page = next((t for t in targets
                             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
            if page is not None:
                state = chrome_scroll(page["webSocketDebuggerUrl"], expression)
                if state is not None and state.get("max"):
                    top = int(round(state.get("top") or 0))
                    want = int(state.get("want") or 0)
                    if abs(top - want) <= 2:
                        return
        time.sleep(0.4)


def elisp_string(value: str) -> str:
    return json.dumps(value)


def save_scroll() -> int:
    """Capture the focused loadout browser's scroll and offer to save it.

    The heavy lookups run first; dmenu is spawned only after the proposed
    percentage is known. Writing is skipped unless the dmenu answer is yes.
    """
    tree = get_tree()
    win = focused_window(tree)
    if win is None:
        print("loadout: no focused window", file=sys.stderr)
        return 1
    ident = next((mark[len(WINDOW_MARK_PREFIX):] for mark in win.marks
                  if mark.startswith(WINDOW_MARK_PREFIX)), None)
    if not ident or not ident.startswith("browser:"):
        print("loadout: focused window is not a loadout browser", file=sys.stderr)
        return 1
    spec = active_spec()
    if spec is None:
        print("loadout: no engaged loadout", file=sys.stderr)
        return 1
    entry = next((e for e in spec.entries if e.ident == ident), None)
    if entry is None or entry.kind != "browser":
        print(f"loadout: no loadout browser entry {ident!r}", file=sys.stderr)
        return 1
    browser_number = int(ident.split(":", 1)[1])
    percent = page_scroll_percent(
        CACHE_DIR / ("chrome-" + ident.replace(":", "_")), entry)
    prompt = f"Save scroll = {percent}% in {spec.source.name}?"
    try:
        choice = subprocess.run(
            ["dmenu", "-p", prompt], input="No\nYes", text=True,
            capture_output=True, timeout=30.0)
    except (OSError, subprocess.TimeoutExpired):
        return 1
    if choice.stdout.strip().lower() not in {"y", "yes"}:
        return 0
    rewrite_scroll(spec.source, browser_number, percent)
    print(f"loadout: saved scroll = {percent}% in {spec.source}")
    return 0


def page_scroll_percent(profile: Path, entry: Entry) -> int:
    """Return the focused browser page's scroll as a rounded percentage.

    Reads the profile's DevToolsActivePort for a live --remote-debugging-port=0
    port, then asks the page for {y: scrollY, max: scrollable height}. A page
    that cannot scroll reports 0%.
    """
    try:
        lines = (profile / "DevToolsActivePort").read_text().splitlines()
    except OSError as exc:
        raise LoadoutError("browser DevTools port unavailable") from exc
    if not lines or not lines[0].isdigit():
        raise LoadoutError("browser DevTools port unavailable")
    port = lines[0]
    try:
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=1.5).read())
    except (OSError, ConnectionError, ValueError) as exc:
        raise LoadoutError("could not enumerate browser pages") from exc
    page = next((t for t in targets
                 if t.get("type") == "page" and t.get("url") == entry.url
                 and t.get("webSocketDebuggerUrl")), None)
    if page is None:
        page = next((t for t in targets
                     if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
    if page is None:
        raise LoadoutError("no browser page found")
    sock: socket.socket | None = None
    try:
        sock, buf = ws_connect(page["webSocketDebuggerUrl"])
        result, _ = cdp_request(
            sock,
            buf,
            1,
            "Runtime.evaluate",
            {
                "expression": (
                    "(() => {"
                    "  const r = document.scrollingElement || document.documentElement;"
                    "  return {y: window.scrollY, max: r.scrollHeight - r.clientHeight};"
                    "})()"
                ),
                "returnByValue": True,
            },
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise LoadoutError("browser returned no scroll position")
        y = value.get("y")
        max_height = value.get("max")
        if not isinstance(y, (int, float)) or not isinstance(max_height, (int, float)):
            raise LoadoutError("browser returned an invalid scroll position")
        if max_height <= 0:
            return 0
        return max(0, min(100, int(round(100 * y / max_height))))
    except (OSError, ConnectionError, ValueError) as exc:
        raise LoadoutError("could not read browser scroll position") from exc
    finally:
        if sock is not None:
            sock.close()


def rewrite_scroll(path: Path, browser_number: int, percent: int) -> None:
    """Set `scroll = "N%"` on the browser_numberth [[browser]] block.

    Edits only the one block's `scroll` line (or inserts one right after its
    `url` line), preserving every other line, comment, and blank verbatim. The
    file is replaced atomically in the same directory so inotify-based watchers
    still fire (IN_MOVED_TO), and the result is re-parsed to guarantee a valid
    loadout.
    """
    try:
        original = path.read_text()
    except OSError as exc:
        raise LoadoutError(f"could not read {path}") from exc
    lines = original.splitlines()
    block_start: int | None = None
    browser_index = 0
    for i, line in enumerate(lines):
        if re.match(r"^\s*\[\[browser\]\]", line):
            if browser_index == browser_number:
                block_start = i
                break
            browser_index += 1
    if block_start is None:
        raise LoadoutError(f"no [[browser]] #{browser_number} in {path}")
    block_end = block_start + 1
    while block_end < len(lines) and not re.match(r"^\s*\[", lines[block_end]):
        block_end += 1
    scroll_index: int | None = None
    url_index: int | None = None
    for i in range(block_start, block_end):
        if scroll_index is None and re.match(r"^\s*scroll\s*=", lines[i]):
            scroll_index = i
        if url_index is None and re.match(r"^\s*url\s*=", lines[i]):
            url_index = i
    value = f'scroll = "{percent}%"'
    if scroll_index is not None:
        indent = re.match(r"^\s*", lines[scroll_index]).group(0)
        lines[scroll_index] = f"{indent}{value}"
    else:
        target = url_index if url_index is not None else block_start
        indent = re.match(r"^\s*", lines[target]).group(0)
        lines.insert(target + 1, f"{indent}{value}")
    rewritten = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    try:
        path.write_text(rewritten)
    except OSError as exc:
        raise LoadoutError(f"could not write {path}") from exc
    try:
        load_spec(path)
    except LoadoutError:
        try:
            path.write_text(original)
        except OSError:
            pass
        raise LoadoutError(f"rejected scroll line would corrupt {path}")


def emacs_plant(path: Path) -> str:
    return f"(my/lock-workspace-to-dir {elisp_string(str(path))})"


def emacs_terminal_plant_file(cwd: Path) -> Path:
    """Write the plant snippet a freshly spawned `emacs -nw` loads via -l.

    A plain `emacs -nw` shares nothing with a running emacs (no reachable
    server, and --eval proved flaky under the full init), so the speed-dial
    workspace plant runs in-process right after init through a -l file.
    """
    content = (
        f"(progn (when (fboundp (quote my/lock-workspace-to-dir)) "
        f"(my/lock-workspace-to-dir {elisp_string(str(cwd))})))"
    )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / "emacs-plant.el"
    path.write_text(content + "\n")
    return path


def terminal_command(entry: Entry, private_title: str) -> list[str]:
    command = list(entry.command)
    if command and Path(command[0]).name == "st":
        return [command[0], "-t", private_title, *command[1:]]
    return command


def kill_windows(victims: list[Window]) -> None:
    """Kill confirmed leftover windows and wait for them to close."""
    for win in victims:
        i3(f"[con_id={win.con_id}] kill")
    if not victims:
        return
    deadline = time.monotonic() + 5.0
    victim_ids = {w.con_id for w in victims}
    while time.monotonic() < deadline:
        live = {w.con_id for w in windows(get_tree())}
        if victim_ids.isdisjoint(live):
            break
        time.sleep(0.05)


def stop_process(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a just-spawned process and fall back to SIGKILL."""
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


SHELL_PROGS = {"mksh", "zsh", "bash", "fish", "sh", "dash", "ksh", "ash"}


def _proc_children(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text()
    except OSError:
        return []
    return [int(p) for p in raw.split() if p.isdigit()]


def _proc_comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _net_wm_pid(xid: int | None) -> int | None:
    """i3's tree carries no pid for st/emacs; ask X for _NET_WM_PID."""
    if xid is None:
        return None
    try:
        result = run(["xprop", "-id", hex(xid), "_NET_WM_PID"], timeout=1.0)
    except LoadoutError:
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.rsplit(None, 1)[-1])
    except (ValueError, IndexError):
        return None


def terminal_cwd(win: Window) -> Path | None:
    """The shell's working directory of a managed terminal window.

    The X client (st) spawns the shell as its only child, so the shell is that
    one child iff it is a known shell program; otherwise (st running a program
    directly, or the process already gone) there is nothing to inspect.
    """
    pid = win.pid or _net_wm_pid(win.xid)
    if pid is None:
        return None
    children = _proc_children(pid)
    if not children:
        return None
    shell = children[0]
    if _proc_comm(shell) not in SHELL_PROGS:
        return None
    try:
        return Path(os.readlink(f"/proc/{shell}/cwd"))
    except OSError:
        return None


def emacs_plant_matches(path: Path) -> bool:
    """Ask the Emacs server whether it is planted on `path`."""
    expr = (
        "(and (boundp 'my/current-workspace-root) "
        f"(string= (directory-file-name my/current-workspace-root) "
        f"{elisp_string(str(path))}))"
    )
    try:
        result = run(["emacsclient", "--eval", expr], timeout=3.0)
    except LoadoutError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "t"


SD_DB = Path.home() / "emacs-speed-dial" / "speed-dial.sqlite"


def planted_db_root() -> Path | None:
    """The plant the speed-dial database records as global_workspace.

    Both emacs anchoring and the standalone shell `plant` write this row; the
    shell command never touches the running emacs, so a DB-only drift is only
    visible here and must be reconciled from the loadout side.
    """
    if not SD_DB.is_file():
        return None
    try:
        result = run(["sqlite3", str(SD_DB),
                      "SELECT value FROM state WHERE key='global_workspace'"], timeout=2.0)
    except LoadoutError:
        return None
    if result.returncode != 0:
        return None
    rows = result.stdout.splitlines()
    return Path(rows[0]) if rows else None


class Controller:
    """One-shot materializer: adopt existing windows and launch missing ones.

    Runs only during `lock`; no daemon is left behind, so nothing is pinned,
    watched, or restarted when a window is closed afterwards.
    """

    def __init__(self, spec: Spec, adopted: dict[str, int] | None = None,
                 *, emacs_driven: bool = False):
        self.spec = spec
        self.adopted = adopted or {}
        self.emacs_driven = emacs_driven
        self.original_workspace: str | None = None
        self.mark = MARK_PREFIX + str(spec.root)

    def launch(self, entry: Entry) -> None:
        before = {w.con_id for w in windows(get_tree())}
        title = "__loadout__" + entry.ident.replace(":", "_") + "__"

        if entry.kind == "terminal":
            command = terminal_command(entry, title)
            if entry.emacs:
                command = [*command, "-l", str(emacs_terminal_plant_file(entry.cwd))]
            env = dict(os.environ)
            if entry.script:
                env["LOADOUT_SCRIPT"] = entry.script
                env["LOADOUT_CWD"] = str(entry.cwd)
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=entry.cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise LoadoutError(f"terminal command not found: {command[0]}") from exc
            win = wait_new(before, pid=proc.pid, title=title)
        elif entry.kind == "browser":
            profile = CACHE_DIR / ("chrome-" + entry.ident.replace(":", "_"))
            app_class = "loadout-" + entry.ident.replace(":", "-")
            command = [
                BROWSER_BIN,
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-debugging-port=0",
                "--allow-file-access-from-files",
                f"--class={app_class}",
                "--app=" + entry.url,
            ]
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=entry.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise LoadoutError(f"browser command not found: {command[0]}") from exc
            try:
                win = wait_new_chrome(before, app_class)
                if not ensure_dark_reader_file_access(
                        profile, wait=True, intended_url=entry.url):
                    raise LoadoutError("could not configure Dark Reader")
                apply_current_chrome_theme(profile)
            except LoadoutError:
                stop_process(proc)
                raise
            if entry.scroll is not None:
                scroll_site(profile, entry)
        else:
            client = run(["emacsclient", "--eval", "t"], timeout=2.0)
            spawned: subprocess.Popen[bytes] | None = None
            if client.returncode == 0:
                frame = f"((name . {elisp_string(title)}))"
                result = run(["emacsclient", "-c", "-n", "-F", frame])
                if result.returncode != 0:
                    raise LoadoutError(result.stderr.strip() or "emacsclient could not create a frame")
                win = wait_new(before, title=title)
                if not self.emacs_driven:
                    # Plant here only when the server is idle. When the LOCK is a
                    # nested call-process from the very emacs we are talking to,
                    # the server is blocked and its replies to --eval requests
                    # get dropped ("connection broken by remote peer"); the plant
                    # would silently never happen and the first heal after the
                    # switch would unload on "planted elsewhere". The emacs
                    # driver therefore passes --emacs-driven and plants itself
                    # AFTER this call-process returns.
                    planted = run(["emacsclient", "--eval", emacs_plant(entry.cwd)])
                    if planted.returncode != 0:
                        raise LoadoutError(planted.stderr.strip() or "could not plant Emacs workspace")
            else:
                # No reachable server: spawn a fresh emacs that owns this
                # loadout. It plants itself inline in the --eval (no separate
                # emacsclient round-trip afterwards — that would race the server
                # while it is still starting up and could be misread as a failed
                # launch, killing the very emacs we just started).
                expr = (
                    "(progn "
                    "(require 'server) "
                    "(unless (server-running-p) (server-start)) "
                    f"{emacs_plant(entry.cwd)} "
                    f"(set-frame-parameter nil 'name {elisp_string(title)}))"
                )
                try:
                    spawned = subprocess.Popen(
                        ["emacs", "--eval", expr],
                        cwd=entry.cwd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except FileNotFoundError as exc:
                    raise LoadoutError("emacs is not installed") from exc
                try:
                    win = wait_new(before, pid=spawned.pid, title=title, timeout=20.0)
                except LoadoutError:
                    stop_process(spawned)
                    raise

        i3(f"[con_id={win.con_id}] mark --add {quote(self.mark)}")
        i3(f"[con_id={win.con_id}] mark --add {quote(WINDOW_MARK_PREFIX + entry.ident)}")
        if win.workspace != entry.slot:
            i3(f"[con_id={win.con_id}] move container to workspace {quote(entry.slot)}")
        # Terminals and loadout browser windows carry their loadout name;
        # emacs is always just Emacs.
        if entry.kind in ("terminal", "browser"):
            rename_window(win, entry.name)

    def claim(self, entry: Entry, con_id: int) -> None:
        """Adopt an existing window as this entry's managed instance."""
        win = next((w for w in windows(get_tree()) if w.con_id == con_id), None)
        if win is None:
            self.launch(entry)
            return
        i3(f"[con_id={win.con_id}] mark --add {quote(self.mark)}")
        i3(f"[con_id={win.con_id}] mark --add {quote(WINDOW_MARK_PREFIX + entry.ident)}")
        if win.workspace != entry.slot:
            i3(f"[con_id={win.con_id}] move container to workspace {quote(entry.slot)}")
        if entry.kind in ("terminal", "browser"):
            rename_window(win, entry.name)
        elif entry.kind == "emacs" and not self.emacs_driven:
            run(["emacsclient", "--eval", emacs_plant(entry.cwd)])

    def initial_launch(self) -> None:
        for entry in self.spec.entries:
            if entry.ident in self.adopted:
                self.claim(entry, self.adopted[entry.ident])
            else:
                self.launch(entry)
        if self.original_workspace:
            i3(f"workspace {quote(self.original_workspace)}")


def lock(filename: str, *, emacs_driven: bool = False) -> int:
    """Materialize the loadout once, then exit.

    One-shot: kill confirmed leftover windows first, then adopt the first emacs,
    launch missing entries, and exit. Nothing runs afterwards — no daemon, no
    watcher, no restarting of closed windows.
    """
    STATE_FILE.unlink(missing_ok=True)
    spec = load_spec(filename)
    tree = get_tree()
    adopted, leftover = plan(spec, tree)
    if leftover and not prompt_replace(leftover):
        print("Loadout not changed.")
        return 0

    clear_marks()
    controller = Controller(spec, adopted, emacs_driven=emacs_driven)
    controller.original_workspace = focused_workspace(tree)
    # Kill the confirmed occupants before anything else: a `load` typed in a
    # terminal that lives on a target workspace kills that terminal, and main()
    # ignores the resulting SIGHUP so the controller survives it. Killing first
    # also means a later failed launch (e.g. an unreachable emacs server) cannot
    # leave the confirmed occupants alive. Victims on the caller's own workspace
    # are killed last for good measure.
    leftover = sorted(leftover, key=lambda w: w.workspace == controller.original_workspace)
    kill_windows(leftover)
    try:
        controller.initial_launch()
    except LoadoutError:
        raise
    try:
        print(f"Locking loadout: {spec.source}")
    except BrokenPipeError:
        pass
    return 0


def unload() -> int:
    STATE_FILE.unlink(missing_ok=True)
    clear_marks()
    print("Loadout unloaded. Windows were left open.")
    return 0


def unload_if_planted_other(dir_name: str) -> int:
    """Unload the engaged loadout when the speed-dial plant moves elsewhere.

    The plant command writes the global_workspace row (and the emacs plant sets
    the same state); a loadout whose emacs entry is anchored on a different
    directory has lost its anchor and must be released right away, not served
    half-loaded until the next switch-away heal notices the drift. Planting the
    loadout's own directory is a no-op (e.g. the plant issued at the end of a
    load); with no emacs entry there is nothing anchored to check.
    """
    spec = active_spec()
    if spec is None:
        return 0
    emacs_entry = next((e for e in spec.entries if e.kind == "emacs"), None)
    if emacs_entry is None:
        return 0
    target = Path(dir_name)
    if os.path.realpath(target) == os.path.realpath(emacs_entry.cwd):
        return 0
    unload()
    print(f"Loadout unloaded: planted {target}, expected {emacs_entry.cwd}")
    return 1


def status() -> int:
    print("unloaded")
    return 1


def read_overlay() -> dict[str, str]:
    """The ws.py window-name overlay: {xid or con_id: displayed label}."""
    try:
        data = json.loads(OVERLAY.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def sweep_discrepancy(spec: Spec, present: dict[str, Window]) -> str | None:
    """First discrepancy found sweeping each of spec's entries, else None.

    An entry is healthy when its managed window exists, sits on its own slot,
    shows its assigned name (terminals only; emacs is never renamed) and is
    anchored on its loadout directory — the terminal shell's cwd, or the
    emacs plant and the speed-dial database. Browser entries carry their
    content (url, scroll) inside the page, so a live window on its slot is
    enough. A terminal whose cwd cannot be inspected is assumed fine. The
    sweep reports the first failing entry and stops; `heal` unloads on
    exactly that signal.
    """
    overlay = read_overlay()
    for entry in spec.entries:
        win = present.get(entry.ident)
        if win is None:
            return f"{entry.kind} {entry.ident}: window missing"
        if win.workspace != entry.slot:
            return (
                f"{entry.kind} {entry.ident}: on workspace {win.workspace}, "
                f"expected {entry.slot}"
            )
        if entry.kind == "terminal":
            shown = overlay.get(str(win.con_id))
            if win.xid is not None:
                shown = overlay.get(str(win.xid)) or shown
            if shown != entry.name:
                return f"terminal {entry.ident}: label {shown!r}, expected {entry.name!r}"
            cwd = terminal_cwd(win)
            if cwd is not None and os.path.realpath(cwd) != os.path.realpath(entry.cwd):
                return f"terminal {entry.ident}: cwd {cwd}, expected {entry.cwd}"
        elif entry.kind == "browser":
            continue
        elif not emacs_plant_matches(entry.cwd):
            return f"emacs {entry.ident}: planted elsewhere"
        else:
            db = planted_db_root()
            if db is not None and os.path.realpath(db) != os.path.realpath(entry.cwd):
                return f"emacs {entry.ident}: speed-dial planted {db}, expected {entry.cwd}"
    return None


def heal() -> int:
    """Sweep the engaged loadout and unload on the first discrepancy.

    Runs on every loadout-workspace switch. With no `loadout:` marks on any
    window this is a no-op. Otherwise the loadout TOML is re-read and every
    entry swept in order (see `sweep_discrepancy`): the first failure unloads
    immediately so all workspace buttons turn red, and the user reloads
    explicitly afterwards. There is no repair step and focus is untouched.
    """
    tree = get_tree()
    if not any(
        any(mark.startswith(MARK_PREFIX) for mark in w.marks)
        for w in windows(tree)
    ):
        return 0
    spec = active_spec()
    if spec is None:
        unload()
        print("Loadout unloaded: loadout file missing or unreadable")
        return 1
    present: dict[str, Window] = {}
    for w in windows(tree):
        for mark in w.marks:
            if mark.startswith(WINDOW_MARK_PREFIX):
                present[mark[len(WINDOW_MARK_PREFIX):]] = w
    why = sweep_discrepancy(spec, present)
    if why is None:
        print(f"Loadout healthy: {spec.source}")
        return 0
    unload()
    print(f"Loadout unloaded: {why}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="materialize an i3 project loadout")
    sub = parser.add_subparsers(dest="subcommand")
    p_lock = sub.add_parser("lock")
    p_lock.add_argument("--emacs-driven", action="store_true",
                        help="the calling emacs owns the loadout's emacs entry; "
                             "create its frame but do not plant (the caller "
                             "plants after this process returns)")
    p_lock.add_argument("file")
    sub.add_parser("unload")
    sub.add_parser("status")
    p_chrome_theme = sub.add_parser("chrome-theme")
    p_chrome_theme.add_argument("theme", choices=("light", "dark"))
    p_heal = sub.add_parser("heal")
    sub.add_parser("save-scroll")
    p_occ = sub.add_parser("_occupied", help=argparse.SUPPRESS)
    p_occ.add_argument("file")
    p_emacs = sub.add_parser("_emacs_path", help=argparse.SUPPRESS)
    p_emacs.add_argument("file")
    p_unload_other = sub.add_parser("_unload_if_other", help=argparse.SUPPRESS)
    p_unload_other.add_argument("dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    # When `load` is typed inside a terminal that lives on a target workspace,
    # killing that terminal closes its pty and hangs up the session, which would
    # SIGHUP this process mid-`kill_windows`. The controller must outlive its
    # own terminal, so ignore the hangup for the whole run.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    argv = list(sys.argv[1:] if argv is None else argv)
    invoked_as = Path(sys.argv[0]).name
    if invoked_as == "lock":
        argv.insert(0, "lock")
    elif invoked_as == "unload":
        argv.insert(0, "unload")

    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "lock":
            return lock(args.file, emacs_driven=args.emacs_driven)
        if args.subcommand == "unload":
            return unload()
        if args.subcommand == "status":
            return status()
        if args.subcommand == "chrome-theme":
            return chrome_theme(args.theme)
        if args.subcommand == "heal":
            return heal()
        if args.subcommand == "save-scroll":
            return save_scroll()
        if args.subcommand == "_occupied":
            spec = load_spec(args.file)
            _, leftover = plan(spec, get_tree())
            description = occupied_describe(leftover)
            if description:
                print(description)
                return 1
            return 0
        if args.subcommand == "_emacs_path":
            spec = load_spec(args.file)
            entry = next((e for e in spec.entries if e.kind == "emacs"), None)
            if entry is not None:
                print(entry.cwd)
                return 0
            return 1
        if args.subcommand == "_unload_if_other":
            return unload_if_planted_other(args.dir)
        build_parser().print_help()
        return 2
    except LoadoutError as exc:
        print(f"loadout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
