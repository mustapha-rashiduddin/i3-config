import copy
import json
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


if __name__ == "__main__":
    unittest.main()
