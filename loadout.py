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
    python3 loadout.py unlock

If this file is exposed on PATH as `lock`/`unlock`, it also understands those
invocation names, so the intended shell UX is simply `lock loadout`.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import select
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
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "i3-loadout"
STATE_FILE = CACHE_DIR / "state.json"
LOG_FILE = CACHE_DIR / "loadout.log"
RENAME_PIPE = Path.home() / ".config/i3/.rename_pipe"
OVERLAY = Path.home() / ".config/i3/window_names.json"


class LoadoutError(RuntimeError):
    pass


class StopRequested(Exception):
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


def windows(tree: dict[str, Any]) -> list[Window]:
    out: list[Window] = []
    for node, workspace in walk(tree):
        xid = node.get("window")
        if xid is None or not workspace or workspace == "__i3_scratch":
            continue
        out.append(Window(
            con_id=int(node["id"]),
            xid=int(xid),
            pid=int(node["pid"]) if node.get("pid") is not None else None,
            title=str(node.get("name") or ""),
            workspace=workspace,
            marks=tuple(node.get("marks") or ()),
        ))
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
        command = item.get("command", ["ghostty", "-e", "fish"])
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


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, path)


def read_state() -> dict[str, Any] | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def active_state() -> dict[str, Any] | None:
    state = read_state()
    if not state:
        return None
    try:
        pid = int(state["pid"])
    except Exception:
        return None
    if pid_alive(pid):
        return state
    STATE_FILE.unlink(missing_ok=True)
    return None


def clear_marks() -> None:
    try:
        all_windows = windows(get_tree())
    except LoadoutError:
        return
    for win in all_windows:
        for mark in win.marks:
            if mark.startswith(MARK_PREFIX):
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


class Controller:
    def __init__(self, spec: Spec, replace_ids: set[int]):
        self.spec = spec
        self.replace_ids = replace_ids
        self.stop = False
        self.original_workspace: str | None = None
        self.previous_workspace: dict[int, str] = {}
        self.last_unprotected: str | None = None
        self.next_spawn = {e.ident: 0.0 for e in spec.entries}
        self.launch_time = {e.ident: 0.0 for e in spec.entries}
        self.by_mark = {e.mark: e for e in spec.entries}

    def write_state(self, status: str) -> None:
        atomic_json(STATE_FILE, {
            "pid": os.getpid(),
            "status": status,
            "loadout": str(self.spec.source),
            "root": str(self.spec.root),
            "protected_slots": sorted(self.spec.slots, key=ROW_KEYS.index),
        })

    def prepare(self) -> None:
        clear_marks()
        tree = get_tree()
        self.original_workspace = focused_workspace(tree)
        victims = [w for w in windows(tree) if w.con_id in self.replace_ids]
        for win in victims:
            # Deliberately target only the windows seen during the confirmation
            # prompt. Workspaces outside the loadout are never touched.
            i3(f"[con_id={win.con_id}] kill")
        if victims:
            deadline = time.monotonic() + 5.0
            victim_ids = {w.con_id for w in victims}
            while time.monotonic() < deadline:
                live = {w.con_id for w in windows(get_tree())}
                if victim_ids.isdisjoint(live):
                    break
                time.sleep(0.05)
            else:
                raise LoadoutError("one or more target windows did not close")

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
            if client.returncode == 0:
                frame = f"((name . {elisp_string(title)}))"
                result = run(["emacsclient", "-c", "-n", "-F", frame])
                if result.returncode != 0:
                    raise LoadoutError(result.stderr.strip() or "emacsclient could not create a frame")
                win = wait_new(before, title=title)
            else:
                expr = (
                    "(progn "
                    f"(set-frame-parameter nil 'name {elisp_string(title)}) "
                    f"{emacs_plant(entry.cwd)})"
                )
                try:
                    proc = subprocess.Popen(
                        ["emacs", "--eval", expr],
                        cwd=entry.cwd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except FileNotFoundError as exc:
                    raise LoadoutError("emacs is not installed") from exc
                win = wait_new(before, pid=proc.pid, title=title, timeout=15.0)

            planted = run(["emacsclient", "--eval", emacs_plant(entry.cwd)])
            if planted.returncode != 0:
                raise LoadoutError(planted.stderr.strip() or "could not plant Emacs workspace")

        i3(f"[con_id={win.con_id}] mark --add {quote(entry.mark)}")
        if win.workspace != entry.slot:
            i3(f"[con_id={win.con_id}] move container to workspace {quote(entry.slot)}")
        rename_window(win, entry.name)
        self.launch_time[entry.ident] = time.monotonic()
        self.next_spawn[entry.ident] = 0.0

    def initial_launch(self) -> None:
        for entry in self.spec.entries:
            self.launch(entry)
        if self.original_workspace:
            i3(f"workspace {quote(self.original_workspace)}")
        self.snapshot()

    def snapshot(self) -> list[Window]:
        tree = get_tree()
        current = windows(tree)
        self.previous_workspace = {w.con_id: w.workspace for w in current}
        focused = focused_workspace(tree)
        if focused and focused not in self.spec.slots:
            self.last_unprotected = focused
        if self.last_unprotected is None:
            self.last_unprotected = next((x for x in ROW_KEYS if x not in self.spec.slots), None)
        return current

    def entry_for(self, win: Window) -> Entry | None:
        for mark in win.marks:
            entry = self.by_mark.get(mark)
            if entry:
                return entry
        return None

    def safe_workspace(self) -> str | None:
        if self.last_unprotected and self.last_unprotected not in self.spec.slots:
            return self.last_unprotected
        return next((x for x in ROW_KEYS if x not in self.spec.slots), None)

    def reconcile(self) -> None:
        current = windows(get_tree())
        seen: set[str] = set()

        # Managed windows are pinned to their declared workspace.
        for win in current:
            entry = self.entry_for(win)
            if not entry:
                continue
            seen.add(entry.ident)
            if win.workspace != entry.slot:
                i3(f"[con_id={win.con_id}] move container to workspace {quote(entry.slot)}")

        # Foreign windows cannot remain in a protected loadout workspace.
        fallback = self.safe_workspace()
        for win in current:
            if win.workspace not in self.spec.slots or self.entry_for(win):
                continue
            old = self.previous_workspace.get(win.con_id)
            destination = old if old and old not in self.spec.slots else fallback
            if destination:
                i3(f"[con_id={win.con_id}] move container to workspace {quote(destination)}")
            else:
                i3(f"[con_id={win.con_id}] move scratchpad")

        # A closed/crashed managed application is recreated. Back off if a bad
        # command exits immediately so the controller cannot create a fork loop.
        now = time.monotonic()
        for entry in self.spec.entries:
            if entry.ident in seen or now < self.next_spawn[entry.ident]:
                continue
            try:
                self.launch(entry)
            except LoadoutError as exc:
                self.next_spawn[entry.ident] = time.monotonic() + 5.0
                print(f"loadout: failed to restore {entry.name}: {exc}", file=sys.stderr, flush=True)

        self.previous_workspace = {w.con_id: w.workspace for w in windows(get_tree())}

    def note_close(self, event: dict[str, Any]) -> None:
        marks = (event.get("container") or {}).get("marks") or []
        for mark in marks:
            entry = self.by_mark.get(mark)
            if not entry:
                continue
            # If a freshly started application immediately dies, give it a few
            # seconds before retrying. Otherwise heal on the close event.
            age = time.monotonic() - self.launch_time[entry.ident]
            self.next_spawn[entry.ident] = time.monotonic() + (5.0 if age < 2.0 else 0.0)

    def handle_event(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        change = event.get("change")
        if change == "focus" and (event.get("current") or {}).get("type") == "workspace":
            slot = (event.get("current") or {}).get("name")
            if slot and slot not in self.spec.slots:
                self.last_unprotected = slot
            return
        if change == "close":
            self.note_close(event)
        if change in {"new", "move", "close"}:
            try:
                self.reconcile()
            except LoadoutError as exc:
                print(f"loadout: reconcile failed: {exc}", file=sys.stderr, flush=True)

    def cleanup(self) -> None:
        clear_marks()
        state = read_state()
        if state and int(state.get("pid", -1)) == os.getpid():
            STATE_FILE.unlink(missing_ok=True)

    def serve(self) -> None:
        self.write_state("starting")
        try:
            self.prepare()
            self.initial_launch()
            self.write_state("ready")
            proc = subprocess.Popen(
                ["i3-msg", "-t", "subscribe", "-m", '["workspace","window","shutdown"]'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            if proc.stdout is None:
                raise LoadoutError("could not subscribe to i3 events")
            while not self.stop and proc.poll() is None:
                ready, _, _ = select.select([proc.stdout], [], [], 10.0)
                if proc.stdout in ready:
                    line = proc.stdout.readline()
                    if line:
                        self.handle_event(line)
                else:
                    # Events are the primary mechanism. This slow safety pass
                    # also handles retry/backoff and any missed IPC event.
                    self.reconcile()
            proc.terminate()
        finally:
            self.cleanup()


def lock(filename: str) -> int:
    state = active_state()
    if state:
        print(f"A loadout is already locked: {state.get('loadout', '?')}")
        print("Run `unlock` first.")
        return 1

    spec = load_spec(filename)
    found = occupied(spec, get_tree())
    if found and not prompt_replace(found):
        print("Loadout not changed.")
        return 0

    # The daemon is started before any confirmed target window is killed. This
    # matters when `lock loadout` is itself typed in a terminal that lives on a
    # target workspace: killing that terminal must not kill the controller.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", buffering=1)
    argv = [sys.executable, str(Path(__file__).resolve()), "_serve", str(spec.source)]
    if found:
        argv += ["--replace", json.dumps([w.con_id for w in found])]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    atomic_json(STATE_FILE, {
        "pid": proc.pid,
        "status": "starting",
        "loadout": str(spec.source),
        "root": str(spec.root),
        "protected_slots": sorted(spec.slots, key=ROW_KEYS.index),
    })
    print(f"Locking loadout: {spec.source}")
    return 0


def unlock() -> int:
    state = active_state()
    if not state:
        clear_marks()
        print("No loadout is locked.")
        return 0
    pid = int(state["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        STATE_FILE.unlink(missing_ok=True)
        clear_marks()
        print("No loadout is locked.")
        return 0

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)
    clear_marks()
    if pid_alive(pid):
        print("Unlock requested; controller is still shutting down.")
    else:
        print("Loadout unlocked. Windows were left open.")
    return 0


def status() -> int:
    state = active_state()
    if not state:
        print("unlocked")
        return 1
    print(f"{state.get('status', 'active')}: {state.get('loadout', '?')}")
    return 0


def serve(filename: str, replace: str | None) -> int:
    spec = load_spec(filename)
    replace_ids: set[int] = set()
    if replace:
        try:
            replace_ids = {int(x) for x in json.loads(replace)}
        except Exception as exc:
            raise LoadoutError("invalid replacement window list") from exc

    controller = Controller(spec, replace_ids)

    def stop(_signum, _frame):
        controller.stop = True
        raise StopRequested()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        controller.serve()
    except StopRequested:
        controller.cleanup()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="materialize and protect an i3 project loadout")
    sub = parser.add_subparsers(dest="subcommand")
    p_lock = sub.add_parser("lock")
    p_lock.add_argument("file")
    sub.add_parser("unlock")
    sub.add_parser("status")
    p_serve = sub.add_parser("_serve", help=argparse.SUPPRESS)
    p_serve.add_argument("file")
    p_serve.add_argument("--replace")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    invoked_as = Path(sys.argv[0]).name
    if invoked_as == "lock":
        argv.insert(0, "lock")
    elif invoked_as == "unlock":
        argv.insert(0, "unlock")

    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "lock":
            return lock(args.file)
        if args.subcommand == "unlock":
            return unlock()
        if args.subcommand == "status":
            return status()
        if args.subcommand == "_serve":
            return serve(args.file, args.replace)
        build_parser().print_help()
        return 2
    except LoadoutError as exc:
        print(f"loadout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
