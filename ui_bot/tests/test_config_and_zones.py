import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ui_bot.config import ConfigError, UiSettings, AtomicConfigStore
from ui_bot.zone_editor import ZoneDraft, ZoneError, ZoneStore


class ConfigTests(unittest.TestCase):
    def test_missing_config_loads_defaults_and_save_is_atomic_with_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = AtomicConfigStore(path)
            loaded = store.load()
            self.assertEqual(loaded, UiSettings())

            loaded = UiSettings(mode="minimap", selected_area="yard",
                                auto_reconnect=False, trail_length=75)
            store.save(loaded)
            store.save(UiSettings(mode="memory", selected_area="yard"))

            self.assertEqual(store.load().mode, "memory")
            self.assertTrue(path.with_suffix(".json.bak").is_file())
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_corrupt_primary_recovers_last_valid_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = AtomicConfigStore(path)
            store.save(UiSettings(mode="minimap"))
            store.save(UiSettings(mode="memory"))
            path.write_text("{broken", encoding="utf-8")

            loaded = store.load()

            self.assertEqual(loaded.mode, "minimap")
            self.assertIn("backup", store.last_warning.lower())

    def test_invalid_config_is_rejected_without_replacing_good_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = AtomicConfigStore(path)
            store.save(UiSettings())
            original = path.read_bytes()

            with self.assertRaises(ConfigError):
                store.save(UiSettings(trail_length=-1))

            self.assertEqual(path.read_bytes(), original)

    def test_read_only_replacement_surfaces_a_config_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = AtomicConfigStore(path)
            with mock.patch("ui_bot.config.os.replace",
                            side_effect=PermissionError("read only")):
                with self.assertRaisesRegex(ConfigError, "save"):
                    store.save(UiSettings())


class ZoneDraftTests(unittest.TestCase):
    def test_add_undo_clear_and_validate_polygon(self):
        draft = ZoneDraft("yard")
        draft.add((0, 0))
        draft.add((10, 0))
        draft.add((10, 10))
        draft.add((0, 10))
        self.assertEqual(len(draft.validate()), 4)
        self.assertEqual(draft.undo(), (0.0, 10.0))
        draft.add((0, 10))
        draft.clear()
        self.assertEqual(draft.points, [])

    def test_rejects_too_few_and_self_intersecting_points(self):
        with self.assertRaisesRegex(ZoneError, "three"):
            ZoneDraft("bad", [(0, 0), (1, 1)]).validate()
        bow = ZoneDraft("bow", [(0, 0), (10, 10), (0, 10), (10, 0)])
        with self.assertRaisesRegex(ZoneError, "intersect"):
            bow.validate()

    def test_zone_store_preserves_other_areas_and_writes_terminal_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "areas.json"
            path.write_text(json.dumps({"cell": 3.0, "areas": {
                "old": {"axes": "xz", "polygon": [[0, 0], [2, 0], [0, 2]]}
            }}), encoding="utf-8")
            store = ZoneStore(path)

            store.save_polygon("new", [(40, 40), (80, 40),
                                        (80, 80), (40, 80)])
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertIn("old", data["areas"])
            self.assertEqual(data["areas"]["new"]["axes"], "xz")
            self.assertEqual(data["areas"]["new"]["shape"], "polygon")
            self.assertEqual(data["areas"]["new"]["points"][2], [80.0, 80.0])
            from minimap_bot import Area
            loaded = Area("new", path=str(path)).load()
            self.assertEqual(tuple(loaded.polygon),
                             ((40.0, 40.0), (80.0, 40.0),
                              (80.0, 80.0), (40.0, 80.0)))
            self.assertTrue(path.with_suffix(".json.bak").exists())


if __name__ == "__main__":
    unittest.main()
