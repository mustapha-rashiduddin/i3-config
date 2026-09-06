#!/usr/bin/env python3
"""Project-local i3 loadouts.

The directory containing the TOML file is the loadout root.

Example loadout:

    [[terminal]]
    slot = "j"
    name = "sql"
    path = "./db"

    [[terminal]]
    slot = "k"
    name = "server"
    path = "."
    command = ["st", "-e", "mksh"]

    [emacs]
    slot = "l"
    path = "."

Run directly as:

    python3 loadout.py lock ./loadout
    python3 loadout.py unload

If this file is exposed on PATH as `lock`/`unload`, it also understands those
invocation names, so the intended shell UX is simply `load <dir>`.

`lock` materializes the loadout once: it adopts the first emacs, launches
missing entries, and drops confirmed leftover windows, then exits. No daemon
watches i3 afterwards — closing any window (terminals included) keeps it
closed. `unload` simply clears the loadout marks.

`heal` is triggered by the i3 keybindings that switch to a loadout workspace.
It finds the engaged loadout via the `loadout:<root>` marks left on the windows
(the root is kept in i3's RAM, never written to a file), re-reads the TOML, and
repairs the layout on demand: each entry is located by its unique
`loadout-win:<ident>` mark, moved back to its slot or relaunched if its window
is gone, asking before killing anything that blocks a workspace.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
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
    terminals = data.get("terminal", [])
    if not isinstance(terminals, list):
        raise LoadoutError("[[terminal]] must be an array of tables")

    for n, item in enumerate(terminals):
        if not isinstance(item, dict):
            raise LoadoutError("each [[terminal]] entry must be a table")
        slot = item.get("slot")
        name = item.get("name")
        path = item.get("path", ".")
        command = item.get("command", ["st", "-e", "mksh"])
        if slot not in ROW_KEYS:
            raise LoadoutError(f"terminal {n}: invalid slot {slot!r}")
        if not isinstance(name, str) or not name.strip():
            raise LoadoutError(f"terminal {n}: name is required")
        if not isinstance(path, str):
            raise LoadoutError(f"terminal {n}: path must be a string")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise LoadoutError(f"terminal {n}: command must be a non-empty array of strings")
        entries.append(Entry(
            ident=f"terminal:{n}",
            kind="terminal",
            slot=slot,
            name=name.strip(),
            cwd=resolve_cwd(root, path),
            command=tuple(command),
        ))

    emacs = data.get("emacs")
    if emacs is not None:
        if not isinstance(emacs, dict):
            raise LoadoutError("[emacs] must be a table")
        slot = emacs.get("slot")
        path = emacs.get("path", ".")
        name = emacs.get("name", "emacs")
        if slot not in ROW_KEYS:
            raise LoadoutError(f"emacs: invalid slot {slot!r}")
        if not isinstance(path, str):
            raise LoadoutError("emacs: path must be a string")
        if not isinstance(name, str) or not name.strip():
            raise LoadoutError("emacs: name must be a non-empty string")
        entries.append(Entry("emacs:0", "emacs", slot, name.strip(), resolve_cwd(root, path)))

    if not entries:
        raise LoadoutError("loadout contains no [[terminal]] or [emacs] entries")
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
        if len(new) == 1:
            return new[0]
        time.sleep(0.05)
    raise LoadoutError("timed out waiting for application window")


def elisp_string(value: str) -> str:
    return json.dumps(value)


def emacs_plant(path: Path) -> str:
    return f"(my/lock-workspace-to-dir {elisp_string(str(path))})"


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


def restore_emacs_plant(path: Path) -> bool:
    """Plant the Emacs server on `path` via the loadout entry function."""
    try:
        result = run(["emacsclient", "--eval", emacs_plant(path)], timeout=5.0)
    except LoadoutError:
        return False
    return result.returncode == 0


class Controller:
    """One-shot materializer: adopt existing windows and launch missing ones.

    Runs only during `lock`; no daemon is left behind, so nothing is pinned,
    watched, or restarted when a window is closed afterwards.
    """

    def __init__(self, spec: Spec, adopted: dict[str, int] | None = None):
        self.spec = spec
        self.adopted = adopted or {}
        self.original_workspace: str | None = None
        self.mark = MARK_PREFIX + str(spec.root)

    def launch(self, entry: Entry) -> None:
        before = {w.con_id for w in windows(get_tree())}
        title = "__loadout__" + entry.ident.replace(":", "_") + "__"
        i3(f"workspace {quote(entry.slot)}")

        if entry.kind == "terminal":
            command = terminal_command(entry, title)
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
                raise LoadoutError(f"terminal command not found: {command[0]}") from exc
            win = wait_new(before, pid=proc.pid, title=title)
        else:
            client = run(["emacsclient", "--eval", "t"], timeout=2.0)
            spawned: subprocess.Popen[bytes] | None = None
            if client.returncode == 0:
                frame = f"((name . {elisp_string(title)}))"
                result = run(["emacsclient", "-c", "-n", "-F", frame])
                if result.returncode != 0:
                    raise LoadoutError(result.stderr.strip() or "emacsclient could not create a frame")
                win = wait_new(before, title=title)
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
        # Only st terminals get renamed; emacs is always just Emacs.
        if entry.kind == "terminal":
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
        if entry.kind == "terminal":
            rename_window(win, entry.name)

    def initial_launch(self) -> None:
        for entry in self.spec.entries:
            if entry.ident in self.adopted:
                self.claim(entry, self.adopted[entry.ident])
            else:
                self.launch(entry)
        if self.original_workspace:
            i3(f"workspace {quote(self.original_workspace)}")


def lock(filename: str) -> int:
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
    controller = Controller(spec, adopted)
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


def status() -> int:
    print("unloaded")
    return 1


def _ask_blockers(spec: Spec, obstacles: list[Window]) -> None:
    """Ask, via i3-nagbar, whether to kill the windows blocking the loadout.

    `heal` runs from an i3 keybinding (exec), so there is no terminal to read an
    answer from; the nagbar buttons re-run heal with `--force` (kill) or do
    nothing.
    """
    by_slot: dict[str, list[str]] = {}
    for win in obstacles:
        by_slot.setdefault(win.workspace, []).append(win.title or str(win.xid))
    slots = "; ".join(f"{s}: {', '.join(names)}" for s, names in sorted(by_slot.items()))
    command = " ".join([
        "i3-msg", "exec", "--no-startup-id",
        f"{sys.executable} {Path(__file__).absolute()} heal --force",
    ])
    subprocess.Popen([
        "i3-nagbar", "-t", "warning",
        "-m", f"Loadout workspace(s) occupied ({slots}). Kill and restore?",
        "-B", "Kill & heal", command,
        "-B", "Leave it", "true",
    ])


def heal(force: bool = False) -> int:
    """Verify and repair the engaged loadout; runs on a loadout-workspace switch.

    If no loadout is engaged (no `loadout:` marks) this is a no-op. Otherwise it
    behaves like `load` re-run: every entry must sit, by its `loadout-win:`
    mark, on its own workspace and the emacs on its slot. When everything is
    fulfilled nothing happens; otherwise missing entries are launched and
    displaced ones moved back. Foreign windows are only a problem on a
    workspace some entry actually needs to move onto; those are killed only
    after an i3-nagbar confirmation (`--force` skips the ask), and extra
    windows sharing an already-healthy slot are never touched.
    Focus is returned to the workspace that was active when heal ran.

    Finishes and exits; nothing watches i3 in between.
    """
    spec = active_spec()
    if spec is None:
        return 0
    tree = get_tree()
    current = windows(tree)
    stayed = focused_workspace(tree)

    emacs_entry = next((e for e in spec.entries if e.kind == "emacs"), None)
    emacs_con_id: int | None = None
    for node, workspace in walk(tree):
        if node.get("window") is not None and window_class(node).lower() == "emacs":
            emacs_con_id = int(node["id"])
            break

    # Membership is per-entry: each managed window carries a unique
    # `loadout-win:<ident>` mark (the `loadout:<root>` beacon migrates between
    # windows by design — i3 keeps one window per mark name — so it is useless
    # for enumeration). The pretty entry names only live in ws.py's rename
    # overlay, never in the i3 tree.
    present: dict[str, Window] = {}
    for w in current:
        for mark in w.marks:
            if mark.startswith(WINDOW_MARK_PREFIX):
                present[mark[len(WINDOW_MARK_PREFIX):]] = w

    managed = {w.con_id for w in present.values()}
    # The emacs may lack its `loadout-win:` mark (e.g. an aborted fallback
    # launch left it orphaned). Such an emacs is about to be claimed below, so
    # do not treat it as an obstacle. Only do this when no window carries the
    # emacs mark; once a marked emacs exists every other emacs is foreign.
    if (emacs_entry is not None and emacs_con_id is not None
            and emacs_entry.ident not in present and emacs_con_id not in managed):
        managed.add(emacs_con_id)

    # A workspace only needs evicting when an entry is missing or displaced and
    # has to move onto it. Extra windows sharing a healthy slot (say a browser
    # sitting on top of the emacs) block nothing and are the user's own
    # business — they must not nag on every switch forever.
    needed = {e.slot for e in spec.entries
              if e.ident not in present or present[e.ident].workspace != e.slot}
    obstacles = [w for w in current if w.workspace in needed and w.con_id not in managed]
    if obstacles and not force:
        _ask_blockers(spec, obstacles)
        return 1

    controller = Controller(spec)
    if obstacles:
        kill_windows(obstacles)

    if emacs_entry is not None:
        win = present.get(emacs_entry.ident)
        if win is None:
            if emacs_con_id is not None:
                controller.claim(emacs_entry, emacs_con_id)
            else:
                controller.launch(emacs_entry)
        elif win.workspace != emacs_entry.slot:
            controller.claim(emacs_entry, win.con_id)

    for entry in spec.entries:
        if entry.kind != "terminal":
            continue
        win = present.get(entry.ident)
        if win is None:
            controller.launch(entry)
        elif win.workspace != entry.slot:
            controller.claim(entry, win.con_id)

    # Internal placement self-healing. Every window now sits on its slot, but
    # a reused terminal may have been cd'd away from its loadout directory
    # and an adopted emacs may be planted on a different directory. Per the
    # shift contract a drifted terminal is restarted in place; a misplanted
    # emacs is replanted via its own entry function. Best-effort: never
    # blocks a switch, never asks. A terminal whose state cannot be inspected
    # (no pid, or st running a program rather than a shell) is left alone.
    for entry in spec.entries:
        if entry.kind != "terminal":
            continue
        win = present.get(entry.ident)
        if win is None:
            continue
        cwd = terminal_cwd(win)
        if cwd is not None and os.path.realpath(cwd) != os.path.realpath(entry.cwd):
            kill_windows([win])
            controller.launch(entry)
            print(f"  terminal {entry.ident}: restarted at {entry.cwd}")

    if emacs_entry is not None:
        win = present.get(emacs_entry.ident)
        if win is not None or emacs_con_id is not None:
            drifted = not emacs_plant_matches(emacs_entry.cwd)
            db_root = planted_db_root()
            if db_root is not None and os.path.realpath(db_root) != os.path.realpath(emacs_entry.cwd):
                drifted = True
            if drifted:
                if restore_emacs_plant(emacs_entry.cwd):
                    print(f"  emacs {emacs_entry.ident}: plant -> {emacs_entry.cwd}")

    if stayed:
        i3(f"workspace {quote(stayed)}")
    print(f"Loadout healthy: {spec.source}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="materialize an i3 project loadout")
    sub = parser.add_subparsers(dest="subcommand")
    p_lock = sub.add_parser("lock")
    p_lock.add_argument("file")
    sub.add_parser("unload")
    sub.add_parser("status")
    p_heal = sub.add_parser("heal")
    p_heal.add_argument("--force", action="store_true")
    p_occ = sub.add_parser("_occupied", help=argparse.SUPPRESS)
    p_occ.add_argument("file")
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
            return lock(args.file)
        if args.subcommand == "unload":
            return unload()
        if args.subcommand == "status":
            return status()
        if args.subcommand == "heal":
            return heal(args.force)
        if args.subcommand == "_occupied":
            spec = load_spec(args.file)
            _, leftover = plan(spec, get_tree())
            description = occupied_describe(leftover)
            if description:
                print(description)
                return 1
            return 0
        build_parser().print_help()
        return 2
    except LoadoutError as exc:
        print(f"loadout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
