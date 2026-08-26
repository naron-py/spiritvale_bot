import json
import os
import tempfile
import unittest
from unittest.mock import patch

from minimap_bot import Area, MemoryEyes, toggle_running


class AreaPolygonIntegrationTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="spiritvale-zone-", suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.points = ((0.0, 0.0), (10.0, 0.0),
                       (10.0, 10.0), (0.0, 10.0))

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_polygon_round_trip_and_return_points(self):
        area = Area("yard", path=self.path, polygon=self.points, axes="xz")
        self.assertTrue(area.save())
        loaded = Area("yard", path=self.path).load()
        self.assertEqual(loaded.axes, "xz")
        self.assertEqual(tuple(loaded.polygon), self.points)
        self.assertTrue(loaded.inside(10.0, 5.0))
        self.assertFalse(loaded.inside(10.01, 5.0))
        self.assertEqual(loaded.nearest(15.0, 5.0), (10.0, 5.0))
        hx, hz = loaded.home(15.0, 5.0)
        self.assertTrue(loaded.safe(hx, hz))
        self.assertLess(hx, 10.0)

    def test_final_stick_guard_redirects_outward_motion(self):
        area = Area("yard", polygon=self.points, axes="xz")
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = area
        eyes.basis = ((1.0, 0.0), (0.0, 1.0))
        eyes.last_pos = (8.0, 5.0)
        eyes.goal = (20.0, 5.0)
        eyes.mode = "chasing"
        eyes.boundary_log_at = 0.0
        sx, sy, blocked = eyes.guard_area_step(1.0, 0.0, now=10.0)
        self.assertTrue(blocked)
        self.assertLess(sx, 0.0)
        self.assertAlmostEqual(sy, 0.0)
        self.assertEqual(eyes.mode, "chasing")

    def test_polygon_must_have_a_safe_interior(self):
        area = Area("thin", path=self.path)
        with self.assertRaisesRegex(ValueError, "too narrow"):
            area.set_polygon(((0, 0), (20, 0), (20, 4), (0, 4)))

    def test_circle_union_step_cannot_cross_gap(self):
        area = Area("islands", circles=((0, 0, 5), (12, 0, 5)))
        allowed, guarded = area.guard_step((0, 0), (12, 0))
        self.assertFalse(allowed)
        self.assertTrue(area.safe(*guarded))

    def test_calibration_does_not_push_near_boundary(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0, 0, 10))
        eyes.owner = 0x10000
        eyes.units = [("player", eyes.owner, 8.0, 0.0, 0.0)]
        eyes.known_players = lambda: [eyes.owner]
        eyes._positions = lambda addrs: {eyes.owner: (8.0, 0.0, 0.0)}

        class Pad:
            def __init__(self):
                self.calls = []

            def stick(self, sx, sy, attack):
                self.calls.append((sx, sy, attack))

        pad = Pad()
        with patch("minimap_bot.MEM_CAL_PUSH_S", 0.01):
            self.assertFalse(eyes.calibrate(pad))
        self.assertFalse(any(sx or sy for sx, sy, _ in pad.calls))

    def test_calibration_recovers_after_pixel_fallback_moved_outside_area(self):
        eyes = MemoryEyes.__new__(MemoryEyes)
        eyes.area = Area("ring", circle=(0, 0, 10))
        eyes.owner = 0x10000
        eyes.units = [("player", eyes.owner, 20.0, 0.0, 0.0)]
        eyes.known_players = lambda: [eyes.owner]
        position = [20.0, 0.0, 0.0]
        eyes._positions = lambda addrs: {eyes.owner: tuple(position)}

        class Pad:
            def __init__(self):
                self.calls = []

            def stick(self, sx, sy, attack):
                self.calls.append((sx, sy, attack))
                if sx or sy:
                    position[0] += sx
                    position[2] += sy

        pad = Pad()
        with patch("minimap_bot.MEM_CAL_PUSH_S", 0.01):
            self.assertTrue(eyes.calibrate(pad))
        self.assertEqual(eyes.me, eyes.owner)
        self.assertIsNotNone(eyes.basis)
        self.assertTrue(any(sx or sy for sx, sy, _ in pad.calls))

    def test_area_resume_skips_unfenced_controller_wake_nudge(self):
        class Pad:
            def __init__(self):
                self.calls = []

            def stick(self, sx, sy, attack):
                self.calls.append((sx, sy, attack))

        class Pets:
            def reset(self):
                pass

        pad, woke = Pad(), []
        paused = toggle_running(True, pad, Pets(),
                                wake=lambda _: woke.append(True), area=object())
        self.assertFalse(paused)
        self.assertFalse(woke)

    def test_saving_with_different_cell_preserves_exact_zones_only(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"cell": 99.0, "areas": {
                "circle": {"shape": "circle", "center": [1, 2], "radius": 5},
                "old-mask": {"shape": "mask", "cells": [[1, 2]]}}}, handle)
        area = Area("yard", path=self.path, polygon=self.points)
        self.assertTrue(area.save())
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertIn("circle", saved["areas"])
        self.assertIn("yard", saved["areas"])
        self.assertNotIn("old-mask", saved["areas"])

    def test_xy_zone_is_not_silently_executed_as_xz(self):
        area = Area("xy", polygon=self.points, axes="xy")
        self.assertFalse(area.runtime_supported)


if __name__ == "__main__":
    unittest.main()
