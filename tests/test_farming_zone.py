import math
import unittest

from farming_zone import CircleZone, PolygonZone, detect_horizontal_axes, filter_targets


class CircleZoneTests(unittest.TestCase):
    def test_inside_outside_and_boundary(self):
        zone = CircleZone((10.0, -5.0), 10.0)
        self.assertTrue(zone.contains((10.0, -5.0)))
        self.assertTrue(zone.contains((20.0, -5.0)))
        self.assertFalse(zone.contains((20.001, -5.0)))
        self.assertTrue(zone.contains((18.0, -5.0), margin=2.0))
        self.assertFalse(zone.contains((18.001, -5.0), margin=2.0))

    def test_nearest_safe_point_and_step_guard(self):
        zone = CircleZone((0.0, 0.0), 10.0)
        self.assertEqual(zone.nearest_safe((15.0, 0.0), margin=2.0), (8.0, 0.0))
        allowed, point = zone.guard_step((7.0, 0.0), (6.0, 0.0), margin=2.0)
        self.assertTrue(allowed)
        self.assertEqual(point, (6.0, 0.0))
        allowed, point = zone.guard_step((7.0, 0.0), (9.0, 0.0), margin=2.0)
        self.assertFalse(allowed)
        self.assertEqual(point, (8.0, 0.0))


class PolygonZoneTests(unittest.TestCase):
    def setUp(self):
        self.zone = PolygonZone(((0.0, 0.0), (10.0, 0.0),
                                 (10.0, 10.0), (0.0, 10.0)))

    def test_inside_outside_and_boundary(self):
        self.assertTrue(self.zone.contains((5.0, 5.0)))
        self.assertFalse(self.zone.contains((11.0, 5.0)))
        self.assertTrue(self.zone.contains((10.0, 5.0)))
        self.assertTrue(self.zone.contains((9.0, 5.0), margin=1.0))
        self.assertFalse(self.zone.contains((9.001, 0.5), margin=1.0))

    def test_concave_notch_is_outside(self):
        zone = PolygonZone(((0.0, 0.0), (8.0, 0.0), (8.0, 8.0),
                            (5.0, 8.0), (5.0, 3.0), (3.0, 3.0),
                            (3.0, 8.0), (0.0, 8.0)))
        self.assertTrue(zone.contains((1.0, 6.0)))
        self.assertFalse(zone.contains((4.0, 6.0)))

    def test_nearest_safe_point(self):
        point = self.zone.nearest_safe((15.0, 5.0), margin=1.0)
        self.assertAlmostEqual(point[0], 9.0, places=6)
        self.assertAlmostEqual(point[1], 5.0, places=6)
        self.assertTrue(self.zone.contains(point, margin=1.0))

    def test_boundary_step_is_redirected(self):
        allowed, point = self.zone.guard_step((8.0, 5.0), (10.5, 5.0), margin=1.0)
        self.assertFalse(allowed)
        self.assertTrue(self.zone.contains(point, margin=1.0))

    def test_step_cannot_cut_across_concave_notch(self):
        zone = PolygonZone(((0, 0), (10, 0), (10, 10), (7, 10),
                            (7, 3), (3, 3), (3, 10), (0, 10)))
        self.assertTrue(zone.contains((2, 8)))
        self.assertTrue(zone.contains((8, 8)))
        allowed, guarded = zone.guard_step((2, 8), (8, 8))
        self.assertFalse(allowed)
        self.assertTrue(zone.contains(guarded))


class TargetAndAxisTests(unittest.TestCase):
    def test_target_filter_rejects_dead_friendly_and_outside(self):
        zone = CircleZone((0.0, 0.0), 10.0)
        targets = [
            {"id": 1, "position": (2.0, 0.0), "alive": True, "hostile": True},
            {"id": 2, "position": (3.0, 0.0), "alive": False, "hostile": True},
            {"id": 3, "position": (4.0, 0.0), "alive": True, "hostile": False},
            {"id": 4, "position": (11.0, 0.0), "alive": True, "hostile": True},
        ]
        kept = filter_targets(
            targets, zone,
            position=lambda item: item["position"],
            eligible=lambda item: item["alive"] and item["hostile"])
        self.assertEqual([item["id"] for item in kept], [1])

    def test_horizontal_axis_detection(self):
        xz = [(0.0, 100.0, 0.0), (2.0, 100.1, 5.0), (4.0, 99.9, 9.0)]
        xy = [(0.0, 0.0, 100.0), (2.0, 5.0, 100.1), (4.0, 9.0, 99.9)]
        self.assertEqual(detect_horizontal_axes(xz), "xz")
        self.assertEqual(detect_horizontal_axes(xy), "xy")


if __name__ == "__main__":
    unittest.main()
