import json
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

    def test_slots_auto_assign_in_declaration_order(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
name = "one"

[[terminal]]
name = "two"

[[terminal]]
name = "three"

[emacs]
''')
            spec = loadout.load_spec(file)
            self.assertEqual([e.slot for e in spec.entries], ["j", "k", "l", ";"])
            self.assertEqual(spec.slots, frozenset({"j", "k", "l", ";"}))

    def test_auto_assign_skips_explicit_slots(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
slot = "k"
name = "explicit"

[[terminal]]
name = "auto"
''')
            spec = loadout.load_spec(file)
            self.assertEqual([e.slot for e in spec.entries], ["k", "j"])

    def test_auto_assign_reuses_early_keys_after_explicit_later_slot(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
slot = ";"
name = "first"

[[terminal]]
name = "second"

[[terminal]]
name = "third"
''')
            spec = loadout.load_spec(file)
            self.assertEqual([e.slot for e in spec.entries], [";", "j", "k"])

    def test_auto_assign_exhausts_row_keys(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text("".join("""
[[terminal]]
name = "t%d"
""" % i for i in range(len(loadout.ROW_KEYS) + 1)))
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

    def test_terminal_emacs_flag_runs_emacs_nw(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
slot = "j"
name = "code"
emacs = true
''')
            spec = loadout.load_spec(file)
            self.assertEqual(spec.entries[0].command, ("st", "-e", "emacs", "-nw"))
            self.assertEqual(spec.entries[0].cwd, Path(td).resolve())
            self.assertIsNone(spec.entries[0].script)

    def test_terminal_emacs_flag_marks_config_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            bad = [
                "emacs = true\ncommand = [\"st\", \"-e\", \"mksh\"]",
                "emacs = true\nscript = \"sqlite3\"",
                "emacs = \"yes\"",
            ]
            for extras in bad:
                file = Path(td) / "loadout"
                file.write_text(f'''
[[terminal]]
slot = "j"
name = "code"
{extras}
''')
                with self.assertRaises(loadout.LoadoutError):
                    loadout.load_spec(file)

    def test_browser_parses_url_name_and_scroll(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[browser]]
slot = "j"
url = "https://docs.python.org/3/library/functions.html"
name = "python docs"
scroll = "40%"
''')
            entry = loadout.load_spec(file).entries[0]
            self.assertEqual(entry.kind, "browser")
            self.assertEqual(entry.url, "https://docs.python.org/3/library/functions.html")
            self.assertEqual(entry.name, "python docs")
            self.assertEqual(entry.scroll, "40%")
            self.assertEqual(entry.slot, "j")

    def test_browser_name_defaults_to_host(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[browser]]
url = "https://developer.mozilla.org/en-US/docs/Web/CSS"
scroll = 800
''')
            entry = loadout.load_spec(file).entries[0]
            self.assertEqual(entry.name, "developer.mozilla.org")
            self.assertEqual(entry.scroll, 800)

    def test_browser_requires_http_url(self):
        with tempfile.TemporaryDirectory() as td:
            for url in (None, "ftp://example.com", "javascript:alert(1)", ""):
                file = Path(td) / "loadout"
                file.write_text(f'''
[[browser]]
url = {None if url is None else '"%s"' % url}
''')
                with self.assertRaises(loadout.LoadoutError):
                    loadout.load_spec(file)

    def test_browser_accepts_file_urls(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[browser]]
url = "file:///home/saifr/Documents/lf/Basics.html#lab25"
''')
            entry = loadout.load_spec(file).entries[0]
            self.assertEqual(entry.url, "file:///home/saifr/Documents/lf/Basics.html#lab25")

    def test_browser_rejects_bad_scroll(self):
        with tempfile.TemporaryDirectory() as td:
            bad = ["120%", "55", "5 0%", True, -5]
            for scroll in bad:
                file = Path(td) / "loadout"
                rendered = "true" if scroll is True else repr(scroll)
                file.write_text(f'''
[[browser]]
url = "https://example.com"
scroll = {rendered}
''')
                with self.assertRaises(loadout.LoadoutError):
                    loadout.load_spec(file)

    def test_browser_slot_auto_assigns_in_declaration_order(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "loadout"
            file.write_text('''
[[terminal]]
name = "t0"

[[browser]]
url = "https://example.com"

[emacs]

[[terminal]]
name = "t1"

[[browser]]
url = "https://example.org"
''')
            spec = loadout.load_spec(file)
            self.assertEqual(
                [(e.kind, e.slot) for e in spec.entries],
                [("terminal", "j"), ("terminal", "k"), ("browser", "l"), ("browser", ";"), ("emacs", "m")],
            )


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

    def test_browser_scroll_expr_pixels(self):
        expression = loadout.browser_scroll_expr(420)
        self.assertIn("r.scrollTop = 420", expression)
        self.assertIn("want", expression)
        self.assertIn("JSON.stringify", expression)

    def test_browser_scroll_expr_percent(self):
        expression = loadout.browser_scroll_expr("55%")
        self.assertIn("max * 0.55", expression)


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


class BrowserHealTests(unittest.TestCase):
    def _win(self, cid, xid, ws, marks):
        return loadout.Window(cid, xid, None, "t", ws, tuple(marks))

    def _engage(self, spec, *wins):
        patch.object(loadout, "get_tree", return_value={}).start()
        patch.object(loadout, "windows", return_value=list(wins)).start()
        patch.object(loadout, "active_spec", return_value=spec).start()
        self.addCleanup(patch.stopall)

    def _entry(self):
        return loadout.Entry("browser:0", "browser", "m", "mozilla", Path("/tmp"),
                             url="https://developer.mozilla.org", scroll="40%")

    def test_browser_healthy_needs_no_label_or_plant(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        win = self._win(1, 100, "m", ("loadout:/tmp", "loadout-win:browser:0"))
        with patch.object(loadout, "unload") as unload:
            self._engage(spec, win)
            self.assertEqual(loadout.heal(), 0)
        unload.assert_not_called()

    def test_missing_browser_window_unloads(self):
        entry = self._entry()
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        beacon = self._win(2, 200, "m", ("loadout:/tmp",))
        with patch.object(loadout, "unload") as unload:
            self._engage(spec, beacon)
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

    def test_launch_emacs_terminal_plants_loadout_root(self):
        entry = loadout.Entry("terminal:0", "terminal", "j", "code", Path("/tmp"),
                              ("st", "-e", "emacs", "-nw"), None, emacs=True)
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        spawned = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.pid = 777
                spawned["argv"] = argv
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
            loadout.Controller(spec).launch(entry)
        a = spawned["argv"]
        self.assertEqual(a[:2], ["st", "-t"])
        self.assertEqual(a[3:5], ["-e", "emacs"])
        self.assertEqual(a[5], "-nw")
        self.assertEqual(a[6], "-l")
        expr = Path(a[a.index("-l") + 1]).read_text()
        self.assertIn("my/lock-workspace-to-dir", expr)
        self.assertIn("/tmp", expr)

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


class BrowserLaunchTests(unittest.TestCase):
    def test_launch_opens_isolated_chrome_app_and_scrolls(self):
        entry = loadout.Entry("browser:0", "browser", "m", "mozilla", Path("/tmp"),
                              url="https://developer.mozilla.org/en-US/docs/Web/CSS", scroll="40%")
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        spawned = {}
        events = []

        class FakePopen:
            def __init__(self, argv, **kwargs):
                spawned["argv"] = argv
                spawned["cwd"] = kwargs.get("cwd")
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        def wait_for_browser(*args, **kwargs):
            events.append("wait")
            return loadout.Window(1, 100, 123, "t", "m", ())

        def ensure_dark_reader(*args, **kwargs):
            events.append("ensure")
            return True

        with patch.object(loadout, "i3"), \
             patch.object(loadout, "get_tree", return_value={}), \
             patch.object(loadout.subprocess, "Popen", side_effect=FakePopen), \
             patch.object(loadout, "ensure_dark_reader_file_access",
                          side_effect=ensure_dark_reader) as ensure, \
             patch.object(loadout, "apply_current_chrome_theme") as apply_theme, \
             patch.object(loadout, "wait_new_chrome", side_effect=wait_for_browser) as wait, \
             patch.object(loadout, "scroll_site") as scroll, \
             patch.object(loadout, "rename_window"):
            loadout.Controller(spec).launch(entry)
        argv = spawned["argv"]
        self.assertEqual(argv[0], loadout.BROWSER_BIN)
        self.assertIn("--user-data-dir=" + str(loadout.CACHE_DIR / "chrome-browser_0"), argv)
        self.assertIn("--remote-debugging-port=0", argv)
        self.assertIn("--allow-file-access-from-files", argv)
        self.assertIn("--class=loadout-browser-0", argv)
        self.assertIn("--app=" + entry.url, argv)
        self.assertEqual(spawned["cwd"], Path("/tmp"))
        wait.assert_called_once_with(set(), "loadout-browser-0")
        ensure.assert_called_once_with(
            loadout.CACHE_DIR / "chrome-browser_0", wait=True, intended_url=entry.url)
        self.assertEqual(events, ["wait", "ensure"])
        apply_theme.assert_called_once_with(loadout.CACHE_DIR / "chrome-browser_0")
        scroll.assert_called_once()

    def test_launch_without_scroll_skips_scrolling(self):
        entry = loadout.Entry("browser:0", "browser", "m", "mozilla", Path("/tmp"),
                              url="https://example.com")
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))

        class FakePopen:
            def __init__(self, argv, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        with patch.object(loadout, "i3"), \
             patch.object(loadout, "get_tree", return_value={}), \
             patch.object(loadout.subprocess, "Popen", side_effect=FakePopen), \
             patch.object(loadout, "ensure_dark_reader_file_access", return_value=True), \
             patch.object(loadout, "apply_current_chrome_theme"), \
             patch.object(loadout, "wait_new_chrome",
                          return_value=loadout.Window(1, 100, 124, "t", "m", ())), \
             patch.object(loadout, "scroll_site") as scroll, \
             patch.object(loadout, "rename_window"):
            loadout.Controller(spec).launch(entry)
        scroll.assert_not_called()

    def test_wait_new_chrome_selects_matching_app_not_help_window(self):
        expected = "loadout-browser-0"
        nodes = [
            ({
                "id": 2,
                "window": 102,
                "name": "Help - Dark Reader",
                "window_properties": {"class": expected, "window_role": "browser"},
            }, "m"),
            ({
                "id": 3,
                "window": 103,
                "name": "Local documentation",
                "window_properties": {"class": expected, "window_role": "pop-up"},
            }, "j"),
        ]
        with patch.object(loadout, "get_tree", return_value={}), \
             patch.object(loadout, "walk", return_value=nodes):
            win = loadout.wait_new_chrome(set(), expected, timeout=0.1)
        self.assertEqual(win.con_id, 3)

    def test_launch_stops_browser_when_dark_reader_setup_fails(self):
        entry = loadout.Entry("browser:0", "browser", "m", "mozilla", Path("/tmp"),
                              url="https://example.com")
        spec = loadout.Spec(Path("/tmp/loadout"), Path("/tmp"), (entry,))
        proc = types.SimpleNamespace()
        with patch.object(loadout, "get_tree", return_value={}), \
             patch.object(loadout.subprocess, "Popen", return_value=proc), \
             patch.object(loadout, "wait_new_chrome",
                          return_value=loadout.Window(1, 100, 123, "t", "m", ())), \
             patch.object(loadout, "ensure_dark_reader_file_access", return_value=False), \
             patch.object(loadout, "stop_process") as stop:
            with self.assertRaisesRegex(loadout.LoadoutError, "configure Dark Reader"):
                loadout.Controller(spec).launch(entry)
        stop.assert_called_once_with(proc)


class DarkReaderTests(unittest.TestCase):
    def test_reload_file_page_waits_for_new_loader_and_restores_xy(self):
        target = {
            "type": "page",
            "url": "file:///tmp/doc.html#section",
            "webSocketDebuggerUrl": "ws://target",
        }
        response = types.SimpleNamespace(read=lambda: json.dumps([target]).encode())
        requests = []
        frame_reads = 0

        class FakeSocket:
            def close(self):
                pass

        def request(sock, buf, request_id, method, params=None, **kwargs):
            nonlocal frame_reads
            requests.append((method, params))
            if method == "Runtime.evaluate":
                expression = params["expression"]
                if expression.startswith("({x:"):
                    return {"result": {"value": {"x": 17, "y": 2500}}}, b""
                if expression.startswith("({ready:"):
                    return {"result": {"value": {"ready": True, "active": True}}}, b""
                return {}, b""
            if method == "Page.getFrameTree":
                frame_reads += 1
                loader = "new" if frame_reads >= 3 else "old"
                return {"frameTree": {"frame": {"loaderId": loader}}}, b""
            return {}, b""

        with patch.object(loadout.urllib.request, "urlopen", return_value=response), \
             patch.object(loadout, "ws_connect", return_value=(FakeSocket(), b"")), \
             patch.object(loadout, "cdp_request", side_effect=request), \
             patch.object(loadout.time, "sleep"):
            loadout.reload_file_pages("9222", True)

        self.assertEqual([method for method, _ in requests], [
            "Runtime.evaluate",
            "Page.getFrameTree",
            "Page.reload",
            "Page.getFrameTree",
            "Page.getFrameTree",
            "Runtime.evaluate",
            "Runtime.evaluate",
        ])
        self.assertEqual(requests[-1][1]["expression"], "window.scrollTo(17, 2500)")

    def test_reload_file_page_reports_setup_reload_failure(self):
        target = {
            "type": "page",
            "url": "file:///tmp/doc.html",
            "webSocketDebuggerUrl": "ws://target",
        }
        response = types.SimpleNamespace(read=lambda: json.dumps([target]).encode())

        class FakeSocket:
            def close(self):
                pass

        def request(sock, buf, request_id, method, params=None, **kwargs):
            if method == "Runtime.evaluate":
                return {"result": {"value": {"x": 0, "y": 10}}}, b""
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"loaderId": "old"}}}, b""
            raise loadout.LoadoutError("reload failed")

        with patch.object(loadout.urllib.request, "urlopen", return_value=response), \
             patch.object(loadout, "ws_connect", return_value=(FakeSocket(), b"")), \
             patch.object(loadout, "cdp_request", side_effect=request):
            with self.assertRaisesRegex(
                    loadout.LoadoutError, "did not finish reloading file:///tmp/doc.html"):
                loadout.reload_file_pages("9222")

    def test_file_access_updates_when_supported_but_inactive(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            (profile / "DevToolsActivePort").write_text(
                "9222\n/devtools/browser/test\n")
            requests = []

            class FakeSocket:
                def __init__(self, name):
                    self.name = name

                def close(self):
                    pass

            def request(sock, buf, request_id, method, params=None, **kwargs):
                requests.append((sock.name, method, params, kwargs))
                if method == "Target.createTarget":
                    return {"targetId": "target"}, b""
                if method == "Runtime.evaluate":
                    return {"result": {"value": {"changed": True}}}, b""
                return {}, b""

            sockets = [(FakeSocket("browser"), b""), (FakeSocket("target"), b"")]
            with patch.object(loadout, "ws_connect", side_effect=sockets), \
                 patch.object(loadout, "cdp_request", side_effect=request), \
                 patch.object(loadout, "reload_file_pages") as reload:
                self.assertTrue(loadout.ensure_dark_reader_file_access(profile))

        evaluate = next(request for request in requests
                        if request[1] == "Runtime.evaluate")
        expression = evaluate[2]["expression"]
        self.assertIn("before.fileAccess?.isActive !== true", expression)
        self.assertIn("before.state !== \"ENABLED\"", expression)
        self.assertIn("chrome.management.setEnabled(id, true", expression)
        reload.assert_called_once_with("9222")

    def test_first_install_cleanup_preserves_intended_page_and_settings_target(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            (profile / "DevToolsActivePort").write_text(
                "9222\n/devtools/browser/test\n")
            closed = []
            targets = [
                {
                    "type": "page",
                    "id": "welcome",
                    "url": "https://darkreader.org/help/",
                },
                {
                    "type": "page",
                    "id": "other",
                    "url": "https://example.com/",
                },
                {
                    "type": "page",
                    "id": "intended",
                    "url": "https://darkreader.org/help/en/",
                },
            ]
            response = types.SimpleNamespace(read=lambda: json.dumps(targets).encode())

            class FakeSocket:
                def close(self):
                    pass

            def request(sock, buf, request_id, method, params=None, **kwargs):
                if method == "Target.createTarget":
                    return {"targetId": "settings"}, b""
                if method == "Runtime.evaluate":
                    return {"result": {"value": {"changed": True}}}, b""
                if method == "Target.closeTarget":
                    closed.append(params["targetId"])
                return {}, b""

            with patch.object(loadout, "ws_connect",
                              side_effect=[(FakeSocket(), b""), (FakeSocket(), b"")]), \
                 patch.object(loadout, "cdp_request", side_effect=request), \
                 patch.object(loadout.urllib.request, "urlopen", return_value=response), \
                 patch.object(loadout, "reload_file_pages") as reload:
                self.assertTrue(loadout.ensure_dark_reader_file_access(
                    profile,
                    wait=True,
                    intended_url="https://darkreader.org/help/en/#install",
                ))

        self.assertEqual(closed, ["welcome", "settings"])
        reload.assert_called_once_with("9222")

    def test_expression_sets_master_state_dark_mode_and_disables_automation(self):
        expression = loadout.dark_reader_expression(False)
        self.assertIn("const wanted = false", expression)
        self.assertIn("local.syncSettings === false", expression)
        self.assertIn("...(before.automation || {}), enabled: false", expression)
        self.assertIn('...(before.theme || {}), mode: 1, engine: "dynamicTheme"', expression)
        self.assertIn("await area.set", expression)
        self.assertIn("stored.enabled === wanted", expression)
        self.assertIn("stored.theme?.mode === 1", expression)
        self.assertIn('stored.theme?.engine === "dynamicTheme"', expression)

    def test_set_theme_uses_existing_worker_and_creates_no_target(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n")
            targets = [
                {
                    "id": "page",
                    "type": "page",
                    "url": "file:///tmp/doc.html",
                    "webSocketDebuggerUrl": "ws://page",
                },
                {
                    "id": "worker",
                    "type": "service_worker",
                    "url": f"chrome-extension://{loadout.DARK_READER_ID}/background/index.js",
                    "webSocketDebuggerUrl": "ws://worker",
                },
            ]
            response = types.SimpleNamespace(read=lambda: json.dumps(targets).encode())
            requests = []
            events = []
            positions = {"page": (17, 2500)}

            class FakeSocket:
                def __init__(self, name):
                    self.name = name

                def close(self):
                    pass

            def request(sock, buf, request_id, method, params=None, **kwargs):
                requests.append((sock.name, method, params, kwargs))
                if method == "Runtime.evaluate":
                    events.append("change")
                    return {"result": {"value": {
                        "enabled": True,
                        "automationEnabled": False,
                        "mode": 1,
                        "engine": "dynamicTheme",
                    }}}, b""
                if method == "Target.closeTarget":
                    return {"success": True}, b""
                return {}, b""

            def capture(port, **kwargs):
                events.append("capture")
                return positions

            sockets = [
                (FakeSocket("probe"), b""),
                (FakeSocket("page"), b""),
                (FakeSocket("worker"), b""),
                (FakeSocket("browser"), b""),
            ]
            with patch.object(loadout, "ws_connect", side_effect=sockets), \
                 patch.object(loadout, "cdp_request", side_effect=request), \
                 patch.object(loadout.urllib.request, "urlopen", return_value=response), \
                 patch.object(loadout, "file_page_scroll_positions", side_effect=capture), \
                 patch.object(loadout, "reload_file_pages") as reload:
                self.assertTrue(loadout.set_dark_reader_theme(profile, True))

        self.assertEqual([(r[0], r[1]) for r in requests], [
            ("page", "ServiceWorker.enable"),
            ("page", "ServiceWorker.startWorker"),
            ("worker", "Runtime.evaluate"),
            ("browser", "Target.closeTarget"),
        ])
        self.assertFalse(any(method == "Target.createTarget" for _, method, _, _ in requests))
        self.assertIn("const wanted = true", requests[2][2]["expression"])
        self.assertEqual(events, ["capture", "change"])
        reload.assert_called_once_with("9222", True, positions, include_web=True)

    def test_set_theme_ignores_stale_offline_profile(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            self.assertFalse(loadout.set_dark_reader_theme(profile, True))

    def test_chrome_theme_updates_each_profile(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            first = cache / "chrome-browser_0"
            second = cache / "chrome-browser_1"
            first.mkdir()
            second.mkdir()
            with patch.object(loadout, "CACHE_DIR", cache), \
                 patch.object(loadout, "set_dark_reader_theme") as set_theme:
                self.assertEqual(loadout.chrome_theme("light"), 0)
                self.assertEqual((cache / "chrome-theme").read_text(), "light\n")
        self.assertEqual(set_theme.call_count, 2)
        set_theme.assert_any_call(first, False)
        set_theme.assert_any_call(second, False)


class SaveScrollTests(unittest.TestCase):
    def _focused_tree(self):
        return {
            "type": "root",
            "nodes": [
                {"type": "workspace", "name": "j", "nodes": [
                    {"id": 1, "window": 100, "pid": 1, "name": "Basics",
                     "focused": True,
                     "marks": ("loadout:/tmp/root", "loadout-win:browser:0")},
                ]},
                {"type": "workspace", "name": "__i3_scratch", "nodes": [
                    {"id": 2, "window": 200, "focused": True},
                ]},
            ],
        }

    def test_focused_window_skips_scratch(self):
        win = loadout.focused_window(self._focused_tree())
        self.assertIsNotNone(win)
        self.assertEqual(win.con_id, 1)
        self.assertEqual(win.workspace, "j")

    def test_focused_window_none_without_focus(self):
        tree = {"type": "root", "nodes": [
            {"type": "workspace", "name": "j", "nodes": [
                {"id": 1, "window": 100, "focused": False},
            ]},
        ]}
        self.assertIsNone(loadout.focused_window(tree))

    def test_window_loadout_ident_from_marks(self):
        win = loadout.Window(1, 100, None, "x", "j", ("loadout-win:browser:0",))
        ident = next((m[len(loadout.WINDOW_MARK_PREFIX):] for m in win.marks
                      if m.startswith(loadout.WINDOW_MARK_PREFIX)), None)
        self.assertEqual(ident, "browser:0")

    def test_window_loadout_ident_none_without_marks(self):
        win = loadout.Window(1, 100, None, "x", "j", ())
        ident = next((m[len(loadout.WINDOW_MARK_PREFIX):] for m in win.marks
                      if m.startswith(loadout.WINDOW_MARK_PREFIX)), None)
        self.assertIsNone(ident)

    def _scroll_response(self, y, max_height):
        response = types.SimpleNamespace(read=lambda: json.dumps([
            {"type": "page", "url": "file:///tmp/doc.html",
             "webSocketDebuggerUrl": "ws://target"}]).encode())

        class FakeSocket:
            def close(self):
                pass

        socket = FakeSocket()

        def request(sock, buf, request_id, method, params=None, **kwargs):
            return {"result": {"value": {"y": y, "max": max_height}}}, b""

        return response, socket, request

    def test_page_scroll_percent_rounds_up(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/x\n")
            entry = loadout.Entry("browser:0", "browser", "j", "doc",
                                  Path(td), url="file:///tmp/doc.html")
            response, fake_socket, request = self._scroll_response(1234, 3000)
            with patch.object(loadout.urllib.request, "urlopen", return_value=response), \
                 patch.object(loadout, "ws_connect",
                              return_value=(fake_socket, b"")), \
                 patch.object(loadout, "cdp_request", side_effect=request):
                self.assertEqual(loadout.page_scroll_percent(profile, entry), 41)

    def test_page_scroll_percent_clamps_to_100(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            (profile / "DevToolsActivePort").write_text("9222\n")
            entry = loadout.Entry("browser:0", "browser", "j", "doc",
                                  Path(td), url="file:///tmp/doc.html")
            response, fake_socket, request = self._scroll_response(3500, 3000)
            with patch.object(loadout.urllib.request, "urlopen", return_value=response), \
                 patch.object(loadout, "ws_connect",
                              return_value=(fake_socket, b"")), \
                 patch.object(loadout, "cdp_request", side_effect=request):
                self.assertEqual(loadout.page_scroll_percent(profile, entry), 100)

    def test_page_scroll_percent_zero_when_not_scrollable(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            (profile / "DevToolsActivePort").write_text("9222\n")
            entry = loadout.Entry("browser:0", "browser", "j", "doc",
                                  Path(td), url="file:///tmp/doc.html")
            response, fake_socket, request = self._scroll_response(0, 0)
            with patch.object(loadout.urllib.request, "urlopen", return_value=response), \
                 patch.object(loadout, "ws_connect",
                              return_value=(fake_socket, b"")), \
                 patch.object(loadout, "cdp_request", side_effect=request):
                self.assertEqual(loadout.page_scroll_percent(profile, entry), 0)

    def test_page_scroll_percent_requires_live_port(self):
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td)
            with self.assertRaises(loadout.LoadoutError):
                loadout.page_scroll_percent(
                    profile,
                    loadout.Entry("browser:0", "browser", "j", "doc", Path(td)))

    def test_rewrite_scroll_replaces_existing_line(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loadout"
            path.write_text('''# a comment
[[browser]]
url = "file:///tmp/doc.html"
name = "doc"
scroll = "10%"

[[terminal]]
name = "sql"
''')
            loadout.rewrite_scroll(path, 0, 62)
            text = path.read_text()
            self.assertIn('scroll = "62%"', text)
            self.assertNotIn('scroll = "10%"', text)
            self.assertTrue(text.startswith("# a comment\n[[browser]]\nurl"))

    def test_rewrite_scroll_inserts_after_url(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loadout"
            path.write_text('''[[browser]]
url = "file:///tmp/doc.html"
name = "doc"

[[terminal]]
name = "sql"
''')
            loadout.rewrite_scroll(path, 0, 40)
            self.assertIn('url = "file:///tmp/doc.html"\nscroll = "40%"', path.read_text())
            self.assertTrue(loadout.load_spec(path).entries[0].scroll == "40%")

    def test_rewrite_scroll_targets_second_browser_block(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loadout"
            path.write_text('''[[browser]]
url = "https://example.com"
name = "first"

[[terminal]]
slot = "j"
name = "sql"

[[browser]]
url = "file:///tmp/doc.html"
name = "doc"
''')
            loadout.rewrite_scroll(path, 1, 33)
            text = path.read_text()
            self.assertNotIn("scroll = \"33%\"", text.split("[[browser]]")[1])
            self.assertIn('file:///tmp/doc.html"\nscroll = "33%"', text)
            entries = loadout.load_spec(path).entries
            self.assertEqual(entries[0].scroll, None)
            self.assertEqual(entries[1].scroll, "33%")

    def _save_flow(self, answer, ident="browser:0", focused=True):
        root = Path(tempfile.mkdtemp())
        (root / "loadout").write_text("[[browser]]\nurl = \"file:///tmp/doc.html\"\nname = \"doc\"\n")
        win = loadout.Window(1, 100, None, "doc", "j",
                             (f"loadout:{root}", f"loadout-win:{ident}"))
        spec = loadout.load_spec(root / "loadout")
        return win, spec, root

    def _tree_with(self, win):
        return {"type": "root", "nodes": [
            {"type": "workspace", "name": "j", "nodes": [
                {"id": win.con_id, "window": 100, "focused": True,
                 "marks": win.marks},
            ]},
        ]}

    def test_save_scroll_yes_rewrites_file(self):
        win, spec, root = self._save_flow("yes")
        with patch.object(loadout, "get_tree", return_value=self._tree_with(win)), \
             patch.object(loadout, "active_spec", return_value=spec), \
             patch.object(loadout, "page_scroll_percent", return_value=55), \
             patch.object(loadout.subprocess, "run") as run, \
             patch.object(loadout, "rewrite_scroll") as rewrite:
            run.return_value.stdout = "yes"
            code = loadout.save_scroll()
        self.assertEqual(code, 0)
        rewrite.assert_called_once_with(spec.source, 0, 55)

    def test_save_scroll_no_skips_write(self):
        win, spec, root = self._save_flow("no")
        with patch.object(loadout, "get_tree", return_value=self._tree_with(win)), \
             patch.object(loadout, "active_spec", return_value=spec), \
             patch.object(loadout, "page_scroll_percent", return_value=55), \
             patch.object(loadout.subprocess, "run") as run, \
             patch.object(loadout, "rewrite_scroll") as rewrite:
            run.return_value.stdout = "n"
            code = loadout.save_scroll()
        self.assertEqual(code, 0)
        rewrite.assert_not_called()

    def test_save_scroll_rejects_non_browser_window(self):
        win, spec, root = self._save_flow("yes", ident="terminal:0", focused=False)
        with patch.object(loadout, "get_tree", return_value=self._tree_with(win)), \
             patch.object(loadout, "active_spec", return_value=spec), \
             patch.object(loadout, "rewrite_scroll") as rewrite, \
             patch.object(loadout.subprocess, "run") as run:
            code = loadout.save_scroll()
        self.assertEqual(code, 1)
        rewrite.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
