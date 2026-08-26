import json
import os
import tempfile
import unittest
from unittest.mock import patch

from zone_recorder import (_find_local_player, Hotkeys, RecordingSession, clear_zone,
                           interactive_record, key_code, record_zone)


class RecordingSessionTests(unittest.TestCase):
    def test_recorder_recovers_stale_class_and_tries_more_than_first_seed(self):
        class MS:
            CLASS_NAMES = {"monster": "MonsterController"}
            saved = None
            seeds = []

            @staticmethod
            def type_classes(mem):
                return {}

            @staticmethod
            def find_classes(mem, wanted=None, progress=None):
                self.assertEqual(wanted, {"monster": "MonsterController"})
                if progress:
                    progress("found class")
                return {"monster": 0xCAFE}

            @staticmethod
            def class_slot_rva(mem, ptr):
                self.assertEqual(ptr, 0xCAFE)
                return 0x1234

            @staticmethod
            def load_rva_cache():
                return {"loot": 0x99}

            @classmethod
            def save_rva_cache(cls, rvas):
                cls.saved = rvas

            @staticmethod
            def world_units(mem, classes=None):
                self.assertEqual(classes, {"monster": 0xCAFE})
                return [("monster", 0x1000, 0.0, 0.0, 0.0),
                        ("monster", 0x2000, 0.0, 0.0, 0.0)]

            @classmethod
            def local_player(cls, mem, seed):
                cls.seeds.append(seed)
                return 0xBEEF if seed == 0x2000 else None

        self.assertEqual(_find_local_player(object(), MS), 0xBEEF)
        self.assertEqual(MS.seeds, [0x1000, 0x2000])
        self.assertEqual(MS.saved, {"loot": 0x99, "monster": 0x1234})

    def test_guided_record_prompts_for_shape_then_name_and_replaces_duplicate(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        calls = []
        prompts = []
        answers = iter(("1", "yard"))

        def ask(prompt):
            prompts.append(prompt)
            return next(answers)

        def recorder(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"cell": 3.0, "areas": {
                    "yard": {"shape": "circle", "axes": "xz",
                             "center": [0.0, 0.0], "radius": 20.0}}}, f)
            with patch("builtins.print") as output:
                self.assertTrue(interactive_record(path=path, input_fn=ask,
                                                   recorder=recorder))
            self.assertIn("mode", prompts[0].lower())
            self.assertIn("name", prompts[1].lower())
            rendered = "\n".join(" ".join(map(str, call.args))
                                   for call in output.call_args_list)
            self.assertIn("replaced automatically", rendered)
            self.assertEqual(calls, [(("polygon", "yard"),
                                      {"radius": None, "replace": True,
                                       "path": path})])
        finally:
            os.unlink(path)

    def test_guided_circle_requests_radius(self):
        calls = []
        answers = iter(("circle", "round camp", "35"))

        def recorder(*args, **kwargs):
            calls.append((args, kwargs))
            return True

        self.assertTrue(interactive_record(input_fn=lambda _: next(answers),
                                           recorder=recorder))
        self.assertEqual(calls[0][0], ("circle", "round camp"))
        self.assertEqual(calls[0][1]["radius"], 35.0)
        self.assertTrue(calls[0][1]["replace"])

    def test_polygon_recording_detects_xz_and_supports_undo(self):
        session = RecordingSession("polygon")
        session.start((0.0, 100.0, 0.0))
        session.add((0.0, 100.0, 0.0))
        session.add((10.0, 100.1, 0.0))
        session.add((10.0, 99.9, 10.0))
        session.add((0.0, 100.0, 10.0))
        self.assertEqual(session.undo(), (0.0, 100.0, 10.0))
        session.add((0.0, 100.0, 10.0))
        axes, points = session.finish()
        self.assertEqual(axes, "xz")
        self.assertEqual(points[-1], (0.0, 10.0))

    def test_xy_detection_fails_before_saving_unsupported_zone(self):
        session = RecordingSession("polygon")
        session.start((0.0, 0.0, 5.0))
        session.add((0.0, 0.0, 5.0))
        session.add((10.0, 0.0, 5.0))
        session.add((10.0, 10.0, 5.0))
        with self.assertRaisesRegex(ValueError, "X/Y.*X/Z"):
            session.finish()

    def test_circle_start_position_becomes_center(self):
        session = RecordingSession("circle", radius=35.0)
        session.start((5.0, 2.0, -7.0))
        session.sample((8.0, 2.1, -2.0))
        axes, center, radius = session.finish()
        self.assertEqual(axes, "xz")
        self.assertEqual(center, (5.0, -7.0))
        self.assertEqual(radius, 35.0)

    def test_hotkey_names_are_configurable(self):
        self.assertEqual(key_code("f2"), 0x71)
        self.assertEqual(key_code("BACKSPACE"), 0x08)
        with self.assertRaises(ValueError):
            key_code("not-a-key")
        with self.assertRaisesRegex(ValueError, "must be different"):
            Hotkeys({"start": "f6", "finish": "f6"}, get_state=lambda _: 0)

    def test_clear_removes_only_the_named_zone(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"cell": 3.0, "areas": {
                    "keep": {"cells": []}, "remove": {"cells": []}}}, f)
            self.assertTrue(clear_zone("remove", path))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(set(json.load(f)["areas"]), {"keep"})
        finally:
            os.unlink(path)

    def test_cancel_works_while_player_position_is_unreadable(self):
        class Mem:
            def close(self):
                pass

        class Keys:
            def hit(self, action):
                return action == "cancel"

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        try:
            with (patch("memscan.Mem", return_value=Mem()),
                  patch("zone_recorder._find_local_player", return_value=0x10000),
                  patch("zone_recorder.Hotkeys", return_value=Keys()),
                  patch("zone_recorder._read_position", return_value=None),
                  patch("zone_recorder.time.sleep",
                        side_effect=AssertionError("cancel was not polled"))):
                self.assertFalse(record_zone("polygon", "yard", path=path))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
