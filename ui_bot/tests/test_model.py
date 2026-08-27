import math
import unittest

from ui_bot.model import (BotSnapshot, BotState, EntitySnapshot, FailureCode,
                          FAILURE_POLICIES, SnapshotError)


class SnapshotModelTests(unittest.TestCase):
    def test_valid_mapping_becomes_an_immutable_snapshot(self):
        raw = {
            "sequence": 7,
            "timestamp": 12.5,
            "state": "RUNNING",
            "connected": True,
            "memory_active": True,
            "source": "memory",
            "bot_state": "chasing",
            "player": {"x": 12.0, "z": -4.0},
            "target": {"id": "84", "name": "Wolf", "x": 15.0, "z": -1.0,
                       "distance": 4.2, "valid": True},
            "entities": [{"id": "84", "kind": "monster", "x": 15.0,
                          "z": -1.0, "name": "Wolf", "valid": True}],
            "zone": {"name": "yard", "kind": "polygon",
                     "points": [[0, 0], [20, 0], [20, 20], [0, 20]],
                     "safety_margin": 5.0, "auto_return": True},
            "path": [[12.0, -4.0], [15.0, -1.0]],
            "log": ["target selected"],
        }

        snapshot = BotSnapshot.from_mapping(raw)

        self.assertEqual(snapshot.state, BotState.RUNNING)
        self.assertEqual(snapshot.player, (12.0, -4.0))
        self.assertEqual(snapshot.target.name, "Wolf")
        self.assertEqual(snapshot.entities[0].entity_id, "84")
        self.assertEqual(snapshot.zone.points[2], (20.0, 20.0))
        with self.assertRaises(Exception):
            snapshot.entities += ()

    def test_non_finite_coordinates_fail_closed(self):
        raw = {"sequence": 1, "timestamp": 1.0, "state": "RUNNING",
               "player": {"x": math.nan, "z": 0.0}}

        with self.assertRaisesRegex(SnapshotError, "finite"):
            BotSnapshot.from_mapping(raw)

    def test_malformed_snapshot_fails_with_a_user_safe_error(self):
        with self.assertRaisesRegex(SnapshotError, "snapshot"):
            BotSnapshot.from_mapping(["not", "a", "mapping"])

    def test_every_failure_has_a_complete_safe_policy(self):
        self.assertEqual(set(FAILURE_POLICIES), set(FailureCode))
        for code, policy in FAILURE_POLICIES.items():
            self.assertTrue(policy.user_message, code)
            self.assertTrue(policy.detection, code)
            self.assertTrue(policy.logged, code)
            self.assertIn(policy.safe_state,
                          {BotState.PAUSED, BotState.DISCONNECTED,
                           BotState.SAFE_STOP})
            self.assertIn("release", policy.safe_action.lower())


class EntityValidationTests(unittest.TestCase):
    def test_impossible_entity_coordinates_are_rejected(self):
        with self.assertRaisesRegex(SnapshotError, "coordinate"):
            EntitySnapshot.from_mapping({"id": "1", "kind": "monster",
                                         "x": 1e12, "z": 0.0})


if __name__ == "__main__":
    unittest.main()
