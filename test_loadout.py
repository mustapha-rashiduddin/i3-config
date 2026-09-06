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
    def test_noop_when_not_engaged(self):
        with patch.object(loadout, "active_spec", return_value=None):
            self.assertEqual(loadout.heal(), 0)

    @patch.object(loadout.Controller, "launch")
    @patch.object(loadout.Controller, "claim")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_force_relaunches_missing_entries(self, get_tree, windows, claim, launch):
        entry = loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"), ("st",))
        emacs = loadout.Entry("emacs:0", "emacs", "l", "emacs", Path("/tmp"))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry, emacs))
        get_tree.return_value = {}
        windows.return_value = []
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(force=True), 0)
        launch.assert_any_call(entry)
        launch.assert_any_call(emacs)

    @patch.object(loadout.Controller, "launch")
    @patch.object(loadout.Controller, "claim")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_heal_keeps_present_marked_terminals(self, get_tree, windows, claim, launch):
        entry = loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"), ("st",))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        get_tree.return_value = {}
        win = loadout.Window(1, 100, None, "x", "j", ("loadout:/tmp", "loadout-win:terminal:0"))
        windows.return_value = [win]
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(force=True), 0)
        launch.assert_not_called()
        claim.assert_not_called()

    @patch.object(loadout.Controller, "launch")
    @patch.object(loadout.Controller, "claim")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_heal_moves_displaced_marked_terminal_back(self, get_tree, windows, claim, launch):
        entry = loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"), ("st",))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        get_tree.return_value = {}
        win = loadout.Window(1, 100, None, "x", "k", ("loadout:/tmp", "loadout-win:terminal:0"))
        windows.return_value = [win]
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(force=True), 0)
        launch.assert_not_called()
        claim.assert_called_once_with(entry, win.con_id)

    @patch.object(loadout, "subprocess")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_obstacles_ask_via_nagbar(self, get_tree, windows, subproc):
        spec = loadout.Spec(
            Path("/tmp/loadout"), Path("/tmp"),
            (loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"), ("st",)),),
        )
        blocked = loadout.Window(1, 100, None, "firefox", "j", ())
        get_tree.return_value = {}
        windows.return_value = [blocked]
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(), 1)
        subproc.Popen.assert_called_once()
        argv = subproc.Popen.call_args[0][0]
        self.assertEqual(argv[0], "i3-nagbar")
        self.assertTrue(any("heal --force" in arg for arg in argv))

    @patch.object(loadout.Controller, "launch")
    @patch.object(loadout, "kill_windows")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_force_kills_obstacles(self, get_tree, windows, kill, launch):
        spec = loadout.Spec(
            Path("/tmp/loadout"), Path("/tmp"),
            (loadout.Entry("terminal:0", "terminal", "j", "sql", Path("/tmp"), ("st",)),),
        )
        blocked = loadout.Window(1, 100, None, "firefox", "j", ())
        get_tree.return_value = {}
        windows.return_value = [blocked]
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(force=True), 0)
        kill.assert_called_once_with([blocked])

    @staticmethod
    def _emacs_node(cid: int, xid: int) -> dict:
        return {
            "id": cid, "window": xid, "name": "__loadout__emacs_0__", "pid": None,
            "marks": [], "window_properties": {"class": "Emacs"},
            "type": "con", "nodes": [], "floating_nodes": [],
        }

    @patch.object(loadout.Controller, "launch")
    @patch.object(loadout.Controller, "claim")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_heal_claims_existing_emacs_instead_of_relaunching(self, get_tree, windows, claim, launch):
        emacs = loadout.Entry("emacs:0", "emacs", "l", "emacs", Path("/tmp"))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        get_tree.return_value = {
            "id": 1, "name": "l", "type": "workspace",
            "nodes": [self._emacs_node(55, 5555)], "floating_nodes": [],
        }
        windows.return_value = []
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(), 0)
        claim.assert_called_once_with(emacs, 55)
        launch.assert_not_called()

    @patch.object(loadout, "subprocess")
    @patch.object(loadout.Controller, "claim")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_extra_window_on_healthy_emacs_slot_does_not_nag(self, get_tree, windows, claim, subproc):
        emacs = loadout.Entry("emacs:0", "emacs", "l", "emacs", Path("/tmp"))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        marked = loadout.Window(556, 5556, None, "__loadout__emacs_0__", "l",
                                ("loadout:/tmp", "loadout-win:emacs:0"))
        foreign = loadout.Window(55, 5555, None, "browser", "l", ())
        get_tree.return_value = {}
        windows.return_value = [marked, foreign]
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(), 0)
        subproc.Popen.assert_not_called()
        claim.assert_not_called()

    @patch.object(loadout, "subprocess")
    @patch.object(loadout.Controller, "claim")
    @patch.object(loadout, "windows")
    @patch.object(loadout, "get_tree")
    def test_foreign_window_blocks_displaced_emacs_slot(self, get_tree, windows, claim, subproc):
        emacs = loadout.Entry("emacs:0", "emacs", "l", "emacs", Path("/tmp"))
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (emacs,))
        marked = loadout.Window(556, 5556, None, "__loadout__emacs_0__", ";",
                                ("loadout:/tmp", "loadout-win:emacs:0"))
        foreign = loadout.Window(55, 5555, None, "terminal", "l", ())
        windows.return_value = [marked, foreign]
        get_tree.return_value = {}
        with patch.object(loadout, "active_spec", return_value=spec):
            self.assertEqual(loadout.heal(), 1)
        subproc.Popen.assert_called_once()
        self.assertEqual(subproc.Popen.call_args[0][0][0], "i3-nagbar")
        claim.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
