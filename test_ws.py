import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import ws


def window_tree():
    return {
        "id": 1,
        "nodes": [
            {
                "id": 2,
                "type": "workspace",
                "name": "j",
                "nodes": [
                    {
                        "id": 42,
                        "window": 100,
                        "window_properties": {"class": "Original"},
                        "nodes": [],
                        "floating_nodes": [],
                    }
                ],
                "floating_nodes": [],
            }
        ],
        "floating_nodes": [],
    }


class CloseEventTests(unittest.TestCase):
    def setUp(self):
        ws._cached_tree = window_tree()
        ws._name_overlay = {"100": "Renamed"}
        ws._snapshot = {
            "workspaces": [{"name": "j", "focused": True}],
            "apps": {"j": ["Renamed"]},
            "sw": 0,
            "status": [],
            "screen_w": 0,
        }
        ws._overlay_gen = 0
        ws._focus_gen = 0
        ws._refresh_event.clear()

    @patch.object(ws, "save_overlay")
    def test_close_removes_renamed_window_without_showing_original(self, save_overlay):
        event = {"change": "close", "container": {"id": 42, "window": 100}}

        ws.handle_event(json.dumps(event))

        self.assertEqual(ws._name_overlay, {})
        self.assertEqual(ws._snapshot["apps"], {"j": []})
        self.assertEqual(ws.collect_windows(ws._cached_tree), [])
        save_overlay.assert_called_once_with()

        ws._rebuild_apps()
        self.assertEqual(ws._snapshot["apps"], {"j": []})

    @patch.object(ws, "save_overlay")
    @patch.object(ws, "load_overlay")
    @patch.object(ws, "get_workspaces", return_value=[{"name": "j", "rect": {"width": 1920}}])
    def test_in_flight_refresh_cannot_restore_closed_window(
            self, get_workspaces, load_overlay, save_overlay):
        stale_tree = copy.deepcopy(ws._cached_tree)
        event = json.dumps({"change": "close", "container": {"id": 42, "window": 100}})

        def close_while_fetching_tree():
            ws.handle_event(event)
            return stale_tree

        with patch.object(ws, "get_tree", side_effect=close_while_fetching_tree):
            ws._refresh()

        self.assertEqual(ws.collect_windows(ws._cached_tree), [])
        self.assertEqual(ws._snapshot["apps"], {"j": []})


class WindowLabelTests(unittest.TestCase):
    def test_cosmic_term_uses_short_label(self):
        node = {
            "window_properties": {"class": "com.system76.CosmicTerm"},
            "nodes": [],
            "floating_nodes": [],
        }

        self.assertEqual(ws.collect_windows(node), ["te"])


class ToggleTests(unittest.TestCase):
    def setUp(self):
        ws._show_ws = False
        ws._status_lock = ("N/A", None)
        ws._status_slow = []
        ws._snapshot = {
            "workspaces": [{"name": "j", "focused": True}],
            "apps": {"j": []},
            "loadout_keys": set(),
            "screen_w": 0,
        }

    def with_toggle(self, contents):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(contents)
            path = f.name
        self.addCleanup(os.unlink, path)
        p = patch.object(ws, "SHOW_WS", path)
        p.start()
        self.addCleanup(p.stop)
        return path

    def test_empty_toggle_keeps_workspaces_hidden(self):
        self.with_toggle(b"")

        self.assertFalse(ws.show_ws())

    def test_toggle_with_content_shows_workspaces(self):
        self.with_toggle(b"1")

        self.assertTrue(ws.show_ws())

    def test_missing_toggle_keeps_workspaces_hidden(self):
        with patch.object(ws, "SHOW_WS", "/nonexistent/.show_ws"):
            self.assertFalse(ws.show_ws())

    def test_tick_flips_the_visible_mode(self):
        self.with_toggle(b"1")

        ws.handle_event(json.dumps({"change": "tick"}))

        self.assertTrue(ws._show_ws)

    def buttons(self, blocks):
        return [b for b in blocks if b.get("name")]

    def test_status_blocks_replace_the_workspace_row_when_hidden(self):
        with patch.object(ws, "time_block", return_value="{ Monday } 05/10/2026 12:00:00"):
            blocks = ws.render(ws.ROW_KEYS)

        self.assertEqual(blocks[-1]["full_text"], "{ Monday } 05/10/2026 12:00:00")
        self.assertNotIn(ws.BOX_WIDTH, [b.get("min_width") for b in blocks])

    def test_status_frame_is_the_buttons_then_the_system_blocks(self):
        with patch.object(ws, "time_block", return_value="clock"):
            blocks = ws.render(ws.ROW_KEYS)
            system = ws.system_blocks()

        self.assertEqual(blocks[-len(system):], system)
        self.assertEqual([b["name"] for b in self.buttons(blocks)], ["ws.j"])

    def test_status_mode_draws_small_stock_workspace_buttons(self):
        ws._snapshot["workspaces"] = [{"name": "j", "focused": True},
                                      {"name": "l"}]

        buttons = self.buttons(ws.render(ws.ROW_KEYS))

        self.assertEqual([b["full_text"] for b in buttons], ["j", "l"])
        self.assertEqual([b["min_width"] for b in buttons],
                         [ws.WS_BUTTON_W] * 2)
        self.assertEqual([b["background"] for b in buttons],
                         [ws.WS_BG[ws.WS_FOCUSED], ws.WS_BG[ws.WS_IDLE]])

    def test_workspaces_without_a_window_are_not_drawn(self):
        ws._snapshot["workspaces"] = [{"name": "j", "focused": True}]

        buttons = self.buttons(ws.render(ws.ROW_KEYS))

        self.assertEqual([b["full_text"] for b in buttons], ["j"])

    def test_urgent_workspace_button_uses_the_stock_urgent_color(self):
        ws._snapshot["workspaces"] = [{"name": "j", "focused": True},
                                      {"name": "k", "urgent": True}]

        buttons = self.buttons(ws.render(ws.ROW_KEYS))

        self.assertEqual(buttons[1]["background"], ws.WS_BG[ws.WS_URGENT])

    def test_workspace_boxes_render_when_shown(self):
        ws._show_ws = True

        blocks = ws.render(ws.ROW_KEYS)

        self.assertEqual([b.get("name") for b in blocks[:len(ws.ROW_KEYS)]],
                         [f"ws.{k}" for k in ws.ROW_KEYS])
        self.assertEqual([b.get("min_width") for b in blocks[:len(ws.ROW_KEYS)]],
                         [ws.BOX_WIDTH] * len(ws.ROW_KEYS))


class LockStateTests(unittest.TestCase):
    @patch.object(ws, "planted_db_root", return_value=None)
    def test_mark_root_locks(self, planted):
        tree = {"nodes": [{"nodes": [{"marks": ["loadout:/tmp/l"]}], "floating_nodes": []}]}

        self.assertEqual(ws.lock_state(tree), ("LOCKED", "/tmp/l"))

    def test_planted_root_unlocks(self):
        with tempfile.TemporaryDirectory() as root:
            open(os.path.join(root, "loadout"), "w").close()
            with patch.object(ws, "planted_db_root", return_value=root):
                self.assertEqual(ws.lock_state(None), ("UNLOCKED", root))

    @patch.object(ws, "planted_db_root", return_value=None)
    def test_nothing_engaged_is_na(self, planted):
        self.assertEqual(ws.lock_state(None), ("N/A", None))


if __name__ == "__main__":
    unittest.main()
