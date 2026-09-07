import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import loadout


class SpecTests(unittest.TestCase):
    def test_file_parent_is_root_and_relative_paths_resolve_from_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "db").mkdir()
            file = root / "loadout"
            file.write_text('''
[[terminal]]
slot = "j"
name = "sql"
path = "./db"

[emacs]
slot = ";"
path = "."
''')
            spec = loadout.load_spec(file)
            self.assertEqual(spec.root, root.resolve())
            self.assertEqual(spec.entries[0].cwd, (root / "db").resolve())
            self.assertEqual(spec.entries[1].cwd, root.resolve())
            self.assertEqual(spec.slots, frozenset({"j", ";"}))

    def test_multiple_terminals_can_share_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
slot = "j"
name = "one"

[[terminal]]
slot = "j"
name = "two"
''')
            spec = loadout.load_spec(file)
            self.assertEqual([entry.slot for entry in spec.entries], ["j", "j"])

    def test_invalid_slot_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
slot = "x"
name = "bad"
''')
            with self.assertRaises(loadout.LoadoutError):
                loadout.load_spec(file)

    def test_terminal_script_is_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
slot = "j"
name = "sql"
script = "sqlite3"

[[terminal]]
slot = "k"
name = "plain"
''')
            spec = loadout.load_spec(file)
            self.assertEqual(spec.entries[0].script, "sqlite3")
            self.assertIsNone(spec.entries[1].script)


class PlanTests(unittest.TestCase):
    spec = None

    def make_spec(self, text):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text(text)
            return loadout.load_spec(file)

    def tree(self, *windows):
        return {
            "id": 1,
            "type": "root",
            "nodes": [
                {"id": 10, "type": "workspace", "name": "m", "nodes": list(windows), "floating_nodes": []},
            ],
            "floating_nodes": [],
        }

    @staticmethod
    def node(cid, xid, cls, name):
        return {"id": cid, "window": xid, "pid": None, "name": name,
                "window_properties": {"class": cls},
                "marks": [], "nodes": [], "floating_nodes": []}

    def test_adopts_first_emacs_when_entry_exists(self):
        spec = self.make_spec('''
[emacs]
slot = "l"
''')
        tree = self.tree(
            self.node(31, 1003, "Emacs", "*scratch*"),
            self.node(32, 1004, "Emacs", "*Messages*"),
            self.node(11, 1001, "st-256color", "nvim"),
        )
        adopted, leftover = loadout.plan(spec, tree)
        self.assertEqual(adopted, {"emacs:0": 31})
        self.assertEqual(leftover, [])

    def test_st_terminals_are_never_adopted(self):
        spec = self.make_spec('''
[[terminal]]
slot = "j"
name = "erd"

[emacs]
slot = "l"
''')
        tree = self.tree(
            self.node(11, 1001, "st-256color", "nvim"),
            self.node(12, 1002, "st", "mksh"),
        )
        adopted, leftover = loadout.plan(spec, tree)
        self.assertEqual(adopted, {})
        self.assertEqual(leftover, [])

    def test_foreign_occupant_in_target_workspace_is_leftover(self):
        spec = self.make_spec('''
[[terminal]]
slot = "j"
name = "erd"
''')
        tree = self.tree(
            self.node(11, 1001, "firefox", "firefox"),
        )
        tree["nodes"][0]["name"] = "j"
        adopted, leftover = loadout.plan(spec, tree)
        self.assertEqual(adopted, {})
        self.assertEqual(len(leftover), 1)
        self.assertEqual(leftover[0].workspace, "j")


class TreeTests(unittest.TestCase):
    def tree(self):
        return {
            "id": 1,
            "type": "root",
            "nodes": [
                {
                    "id": 10,
                    "type": "workspace",
                    "name": "j",
                    "nodes": [
                        {
                            "id": 11,
                            "window": 1001,
                            "pid": 51,
                            "name": "target",
                            "marks": [],
                            "nodes": [],
                            "floating_nodes": [],
                        }
                    ],
                    "floating_nodes": [],
                },
                {
                    "id": 20,
                    "type": "workspace",
                    "name": "m",
                    "focused": True,
                    "nodes": [
                        {
                            "id": 21,
                            "window": 1002,
                            "pid": 52,
                            "name": "leave-alone",
                            "marks": [],
                            "nodes": [],
                            "floating_nodes": [],
                        }
                    ],
                    "floating_nodes": [],
                },
            ],
            "floating_nodes": [],
        }

    def test_occupied_only_finds_declared_workspaces(self):
        entry = loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"), ("st",))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        found = loadout.occupied(spec, self.tree())
        self.assertEqual([window.title for window in found], ["target"])

    def test_special_slot_characters_are_quoted_for_i3(self):
        self.assertEqual(loadout.quote(";"), '";"')
        self.assertEqual(loadout.quote(","), '","')


class HelperTests(unittest.TestCase):
    def test_st_gets_private_title_for_discovery(self):
        entry = loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"), ("st", "-e", "mksh"))
        self.assertEqual(
            loadout.terminal_command(entry, "marker"),
            ["st", "-t", "marker", "-e", "mksh"],
        )

    def test_emacs_expression_plants_exact_path(self):
        expression = loadout.emacs_plant(Path('/tmp/a "quoted" dir'))
        self.assertIn("my/lock-workspace-to-dir", expression)
        self.assertIn('\\"quoted\\"', expression)


class ActiveSpecTests(unittest.TestCase):
    def test_no_marks_means_no_active_loadout(self):
        with patch.object(loadout, "windows", return_value=[]):
            self.assertIsNone(loadout.active_spec())

    def test_reads_root_from_marks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "loadout").write_text('''
[[terminal]]
slot = "j"
name = "sql"

[emacs]
slot = "l"
''')
            win = loadout.Window(1, 100, None, "sql", "j", (f"loadout:{root}",))
            with patch.object(loadout, "windows", return_value=[win]):
                spec = loadout.active_spec()
            self.assertEqual(spec.source, root / "loadout")
            self.assertEqual(spec.entries[0].name, "sql")

    def test_legacy_ident_marks_are_ignored(self):
        win = loadout.Window(2, 101, None, "x", "j", ("loadout:terminal:0",))
        with patch.object(loadout, "windows", return_value=[win]):
            self.assertIsNone(loadout.active_spec())


class HealTests(unittest.TestCase):
    def _entry(self, ident="terminal:0", kind="terminal", slot="j", name="sql", cwd=None):
        if kind == "emacs":
            return loadout.Entry(ident, kind, slot, name, cwd or Path("/tmp"))
        return loadout.Entry(ident, kind, slot, name, cwd or Path("/tmp"), ("st", "-e", "mksh"))

    def _win(self, cid, xid, ws, marks):
        return loadout.Window(cid, xid, None, "t", ws, tuple(marks))

    def _engage(self, spec, *wins):
        get_tree = patch.object(loadout, "get_tree", return_value={}).start()
        windows = patch.object(loadout, "windows", return_value=list(wins)).start()
        active = patch.object(loadout, "active_spec", return_value=spec).start()
        self.addCleanup(patch.stopall)

    def test_noop_when_not_engaged(self):
        patch.object(loadout, "get_tree", return_value={}).start()
        patch.object(loadout, "windows", return_value=[]).start()
        unload = patch.object(loadout, "unload").start()
        self.addCleanup(patch.stopall)
        self.assertEqual(loadout.heal(), 0)
        unload.assert_not_called()

    def test_unreadable_spec_unloads(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        patch.object(loadout, "get_tree", return_value={}).start()
        patch.object(loadout, "windows", return_value=[self._win(1, 100, "j", ("loadout:/tmp",))]).start()
        patch.object(loadout, "active_spec", return_value=None).start()
        unload = patch.object(loadout, "unload").start()
        self.addCleanup(patch.stopall)
        self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_healthy_loadout_stays_green(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "j", ("loadout:/tmp", "loadout-win:terminal:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "read_overlay", return_value={"100": "sql"}), \
             patch.object(loadout, "terminal_cwd", return_value=Path("/tmp")):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 0)
        unload.assert_not_called()

    def test_missing_window_unloads(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        beacon = self._win(2, 200, "m", ("loadout:/tmp",))
        with patch.object(loadout, "unload") as unload:
            self._engage(spec, beacon)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_wrong_workspace_unloads(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "k", ("loadout:/tmp", "loadout-win:terminal:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "read_overlay", return_value={"100": "sql"}), \
             patch.object(loadout, "terminal_cwd", return_value=Path("/tmp")):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_wrong_name_unloads(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "j", ("loadout:/tmp", "loadout-win:terminal:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "read_overlay", return_value={"100": "server"}), \
             patch.object(loadout, "terminal_cwd", return_value=Path("/tmp")):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_missing_overlay_name_unloads(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "j", ("loadout:/tmp", "loadout-win:terminal:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "read_overlay", return_value={}), \
             patch.object(loadout, "terminal_cwd", return_value=Path("/tmp")):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_cwd_drift_unloads(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "j", ("loadout:/tmp", "loadout-win:terminal:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "read_overlay", return_value={"100": "sql"}), \
             patch.object(loadout, "terminal_cwd", return_value=Path("/elsewhere")):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_uninspectable_cwd_is_healthy(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "j", ("loadout:/tmp", "loadout-win:terminal:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "read_overlay", return_value={"100": "sql"}), \
             patch.object(loadout, "terminal_cwd", return_value=None):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 0)
        unload.assert_not_called()

    def test_healthy_emacs_stays_green(self):
        emacs = self._entry("emacs:0", "emacs", "l", "emacs")
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        win = self._win(556, 5556, "l", ("loadout:/tmp", "loadout-win:emacs:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "emacs_plant_matches", return_value=True), \
             patch.object(loadout, "planted_db_root", return_value=Path("/tmp")):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 0)
        unload.assert_not_called()

    def test_emacs_misplant_unloads(self):
        emacs = self._entry("emacs:0", "emacs", "l", "emacs")
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        win = self._win(556, 5556, "l", ("loadout:/tmp", "loadout-win:emacs:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "emacs_plant_matches", return_value=False), \
             patch.object(loadout, "planted_db_root", return_value=None):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_emacs_db_drift_unloads(self):
        emacs = self._entry("emacs:0", "emacs", "l", "emacs")
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        win = self._win(556, 5556, "l", ("loadout:/tmp", "loadout-win:emacs:0"))
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "emacs_plant_matches", return_value=True), \
             patch.object(loadout, "planted_db_root", return_value=Path("/elsewhere")):
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()

    def test_unload_if_other_noop_when_not_engaged(self):
        patch.object(loadout, "active_spec", return_value=None).start()
        unload = patch.object(loadout, "unload").start()
        self.addCleanup(patch.stopall)
        self.assertEqual(loadout.unload_if_planted_other("/elsewhere"), 0)
        unload.assert_not_called()

    def test_unload_if_other_noop_without_emacs_entry(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        patch.object(loadout, "active_spec", return_value=spec).start()
        unload = patch.object(loadout, "unload").start()
        self.addCleanup(patch.stopall)
        self.assertEqual(loadout.unload_if_planted_other("/elsewhere"), 0)
        unload.assert_not_called()

    def test_unload_if_other_noop_on_same_directory(self):
        emacs = self._entry("emacs:0", "emacs", "l", "emacs", cwd=Path("/tmp"))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        patch.object(loadout, "active_spec", return_value=spec).start()
        unload = patch.object(loadout, "unload").start()
        self.addCleanup(patch.stopall)
        self.assertEqual(loadout.unload_if_planted_other("/tmp/"), 0)
        unload.assert_not_called()

    def test_unload_if_other_releases_on_different_directory(self):
        emacs = self._entry("emacs:0", "emacs", "l", "emacs", cwd=Path("/tmp"))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        patch.object(loadout, "active_spec", return_value=spec).start()
        unload = patch.object(loadout, "unload").start()
        self.addCleanup(patch.stopall)
        self.assertEqual(loadout.unload_if_planted_other("/elsewhere"), 1)
        unload.assert_called_once_with()

    def test_extra_window_on_healthy_slot_does_not_unload(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "j", ("loadout:/tmp", "loadout-win:terminal:0"))
        foreign = self._win(2, 200, "j", ())
        with patch.object(loadout, "unload") as unload, \
             patch.object(loadout, "read_overlay", return_value={"100": "sql"}), \
             patch.object(loadout, "terminal_cwd", return_value=Path("/tmp")):
            self._engage(spec, win, foreign)
            self.assertEqual(loadout.heal(), 0)
        unload.assert_not_called()

    def test_first_discrepancy_stops_the_sweep(self):
        first = self._entry("terminal:0", "terminal", "j", "sql")
        second = self._entry("terminal:1", "terminal", "k", "server")
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (first, second))
        win0 = self._win(1, 100, "k", ("loadout:/tmp", "loadout-win:terminal:0"))
        win1 = self._win(3, 300, "k", ("loadout:/tmp", "loadout-win:terminal:1"))
        with patch.object(loadout, "unload") as unload:
            self._engage(spec, win0, win1)
            self.assertEqual(loadout.heal(), 1)
        unload.assert_called_once_with()


class EmacsLaunchTests(unittest.TestCase):
    def _entry(self) -> loadout.Entry:
        return loadout.Entry("emacs:0", "emacs", "l", "emacs", Path("/tmp"))

    def _setup(self):
        get_tree = patch.object(loadout, "get_tree", return_value={}).start()
        windows = patch.object(loadout, "windows", return_value=[]).start()
        i3 = patch.object(loadout, "i3").start()
        self.addCleanup(patch.stopall)
        return i3

    @staticmethod
    def _proc_class(records):
        class FakeProc:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.pid = 999
                self.terminated = False
                records.append(self)
            def terminate(self):
                self.terminated = True
            def kill(self):
                self.terminated = True
            def wait(self, timeout=None):
                pass
        return FakeProc

    def test_no_server_spawns_self_planting_emacs_without_extra_emacsclient(self):
        i3 = self._setup()
        spawns = []
        with patch.object(loadout, "run", return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="")), \
             patch.object(loadout.subprocess, "Popen", side_effect=self._proc_class(spawns)), \
             patch.object(loadout, "wait_new", return_value=loadout.Window(1, 100, 999, "t", "l", ())):
            loadout.Controller(loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (self._entry(),))).launch(self._entry())

        self.assertEqual(len(spawns), 1)
        self.assertEqual(spawns[0].argv[0], "emacs")
        self.assertEqual(spawns[0].argv[1], "--eval")
        expr = spawns[0].argv[2]
        self.assertIn("(server-start)", expr)
        self.assertIn("my/lock-workspace-to-dir", expr)
        self.assertLess(expr.index("my/lock-workspace"), expr.index("set-frame-parameter"))
        commands = [c.args[0] for c in i3.call_args_list]
        self.assertTrue(any("loadout-win:emacs:0" in c for c in commands))

    def test_reachable_server_creates_frame_and_plants_via_emacsclient(self):
        i3 = self._setup()
        calls = []
        fake = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        def fake_run(argv, timeout=5.0):
            calls.append(argv)
            return fake
        with patch.object(loadout, "run", side_effect=fake_run), \
             patch.object(loadout.subprocess, "Popen") as popen, \
             patch.object(loadout, "wait_new", return_value=loadout.Window(1, 100, None, "t", "l", ())):
            loadout.Controller(loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (self._entry(),))).launch(self._entry())

        popen.assert_not_called()
        self.assertTrue(any(a[:2] == ["emacsclient", "-c"] for a in calls))
        plants = [a for a in calls if a[0] == "emacsclient" and "--eval" in a]
        self.assertTrue(any("my/lock-workspace-to-dir" in a[2] for a in plants))

    def test_spawn_path_terminates_emacs_when_window_never_appears(self):
        self._setup()
        spawns = []
        run = patch.object(loadout, "run", return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="")).start()
        popen = patch.object(loadout.subprocess, "Popen", side_effect=self._proc_class(spawns)).start()
        wait_new = patch.object(loadout, "wait_new", side_effect=loadout.LoadoutError("timeout")).start()
        with self.assertRaises(loadout.LoadoutError):
            loadout.Controller(loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (self._entry(),))).launch(self._entry())
        self.assertTrue(spawns[0].terminated)


class TerminalLaunchTests(unittest.TestCase):
    def _entry(self, ident="terminal:0") -> loadout.Entry:
        return loadout.Entry(ident, "terminal", "j", "sql", Path("/tmp"),
                             ("st", "-e", "mksh"), "sql")

    def test_launch_exports_script_env_var(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        envs = []

        class FakePopen:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.pid = 777
                envs.append(kwargs.get("env"))
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        with patch.object(loadout, "i3"), \
             patch.object(loadout, "get_tree", return_value={}), \
             patch.object(loadout.subprocess, "Popen", side_effect=FakePopen), \
             patch.object(loadout, "wait_new",
                          return_value=loadout.Window(1, 100, 777, "t", "j", ())), \
             patch.object(loadout, "rename_window"):
            loadout.Controller(spec).launch(self._entry())
        self.assertEqual(envs[0].get("LOADOUT_SCRIPT"), "sql")
        self.assertEqual(envs[0].get("LOADOUT_CWD"), str(Path("/tmp")))

    def test_launch_without_script_leaves_env_alone(self):
        entry = loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"),
                              ("st", "-e", "mksh"), None)
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        envs = []

        class FakePopen:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.pid = 778
                envs.append(kwargs.get("env"))
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        with patch.object(loadout, "i3"), \
             patch.object(loadout, "get_tree", return_value={}), \
             patch.object(loadout.subprocess, "Popen", side_effect=FakePopen), \
             patch.object(loadout, "wait_new",
                          return_value=loadout.Window(1, 100, 778, "t", "j", ())), \
             patch.object(loadout, "rename_window"):
            loadout.Controller(spec).launch(entry)
        self.assertIsNone(envs[0].get("LOADOUT_SCRIPT"))

    def test_launch_moves_window_to_slot_without_switching_workspace(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        commands = []

        class FakePopen:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.pid = 999
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        with patch.object(loadout, "i3", side_effect=commands.append), \
             patch.object(loadout, "get_tree", return_value={}), \
             patch.object(loadout.subprocess, "Popen",
                          return_value=FakePopen(["st", "-e", "mksh"])), \
             patch.object(loadout, "wait_new",
                          return_value=loadout.Window(1, 100, 999, "t", "m", ())), \
             patch.object(loadout, "rename_window"):
            loadout.Controller(spec).launch(entry)
        self.assertFalse(any(c.startswith("workspace ") for c in commands), commands)
        self.assertTrue(any(f"move container to workspace {loadout.quote('j')}" in c for c in commands),
                        commands)


if __name__ == "__main__":
    unittest.main()
