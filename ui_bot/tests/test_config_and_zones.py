import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ui_bot.config import (AttackSlot, BuffSlot, ConfigError, UiSettings,
                           AtomicConfigStore, default_attack_slots,
                           default_buff_slots)
from ui_bot.zone_editor import ZoneDraft, ZoneError, ZoneStore


class ConfigTests(unittest.TestCase):
    def test_default_controller_slots_match_the_six_buffs_and_two_attacks(self):
        settings = UiSettings()

        self.assertEqual(
            [slot.button for slot in settings.buff_slots],
            ["dpad_up", "dpad_down", "dpad_left", "dpad_right", "x", "a"])
        self.assertTrue(all(slot.enabled for slot in settings.buff_slots))
        self.assertEqual(
            [(slot.button, slot.enabled) for slot in settings.attack_slots],
            [("lb", True), ("rb", True)])

    def test_active_binding_conflicts_name_both_slots_but_disabled_slots_do_not(self):
        buffs = list(default_buff_slots())
        buffs[1] = BuffSlot(**{**buffs[1].__dict__, "button": "dpad_up"})
        with self.assertRaisesRegex(ConfigError, "Buff Slot 1.*Buff Slot 2"):
            UiSettings(buff_slots=tuple(buffs)).validated()

        buffs[1] = BuffSlot(**{
            **buffs[1].__dict__, "enabled": False, "button": "lb"})
        UiSettings(buff_slots=tuple(buffs)).validated()

        attacks = list(default_attack_slots())
        attacks[0] = AttackSlot(**{**attacks[0].__dict__, "button": "dpad_up"})
        with self.assertRaisesRegex(ConfigError, "Buff Slot 1.*Attack Skill 1"):
            UiSettings(attack_slots=tuple(attacks)).validated()

    def test_at_least_one_attack_skill_must_be_enabled(self):
        attacks = tuple(AttackSlot(**{**slot.__dict__, "enabled": False})
                        for slot in default_attack_slots())
        with self.assertRaisesRegex(ConfigError, "at least one"):
            UiSettings(attack_slots=attacks).validated()

    def test_attack_configuration_has_exactly_two_slots(self):
        extra = AttackSlot("attack-extra", "Attack Skill 3", True, "a", 2)
        with self.assertRaisesRegex(ConfigError, "exactly two"):
            UiSettings(attack_slots=default_attack_slots() + (extra,)).validated()

    def test_loaded_extra_attack_slot_is_reported_and_dropped(self):
        raw = UiSettings().control_config()
        raw["schema"] = 2
        raw["trail_length"] = 321
        raw["attack_slots"].append({
            "id": "attack-extra", "name": "Attack Skill 3", "enabled": True,
            "button": "a", "order": 2})

        loaded, warnings = UiSettings.parse(raw)

        self.assertEqual(loaded.trail_length, 321)
        self.assertEqual(loaded.attack_slots, default_attack_slots())
        self.assertTrue(any("extra attack slot" in warning for warning in warnings))

    def test_dynamic_slot_round_trips_with_stable_id_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = AtomicConfigStore(path)
            dynamic = BuffSlot("buff-user-123", "Shield", False, "rt", 6,
                               user_created=True)
            settings = UiSettings(buff_slots=default_buff_slots() + (dynamic,))

            store.save(settings)
            loaded = store.load()

            self.assertEqual(loaded.buff_slots[-1], dynamic)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], settings.schema)
            self.assertEqual(raw["buff_slots"][-1]["id"], "buff-user-123")
            self.assertEqual(raw["buff_slots"][-1]["order"], 6)

    def test_one_invalid_default_slot_falls_back_without_erasing_other_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            payload = UiSettings().__dict__.copy()
            payload["buff_slots"] = [slot.__dict__.copy()
                                     for slot in default_buff_slots()]
            payload["attack_slots"] = [slot.__dict__.copy()
                                       for slot in default_attack_slots()]
            payload["buff_slots"][0]["button"] = "not-a-controller-button"
            payload["buff_slots"][1]["enabled"] = False
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = AtomicConfigStore(path)
            loaded = store.load()

            self.assertEqual(loaded.buff_slots[0], default_buff_slots()[0])
            self.assertFalse(loaded.buff_slots[1].enabled)
            self.assertIn("Buff Slot 1", store.last_warning)

    def test_one_conflicting_loaded_slot_falls_back_without_erasing_other_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            payload = UiSettings().__dict__.copy()
            payload["buff_slots"] = [slot.__dict__.copy()
                                     for slot in default_buff_slots()]
            payload["attack_slots"] = [slot.__dict__.copy()
                                       for slot in default_attack_slots()]
            payload["trail_length"] = 333
            payload["buff_slots"][1]["button"] = "dpad_up"
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = AtomicConfigStore(path)
            loaded = store.load()

            self.assertEqual(loaded.trail_length, 333)
            self.assertEqual(loaded.buff_slots[1].button, "dpad_down")
            self.assertIn("Buff Slot 2 conflict", store.last_warning)

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

    def test_failed_save_does_not_replace_valid_backup_with_bad_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicConfigStore(Path(directory) / "settings.json")
            store.save(UiSettings(selected_area="last-good"))
            store.backup.write_bytes(store.path.read_bytes())
            backup_before = store.backup.read_bytes()
            store.path.write_text("{broken", encoding="utf-8")

            with mock.patch("ui_bot.config.os.replace", side_effect=OSError("busy")):
                with self.assertRaises(ConfigError):
                    store.save(UiSettings(selected_area="new"))

            self.assertEqual(store.backup.read_bytes(), backup_before)

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
