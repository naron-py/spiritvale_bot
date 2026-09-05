from contextlib import redirect_stdout
import io
import json
import queue
import threading
import time
import unittest

from ui_bot.runtime_child import (CommandGate, JsonDashboard, SNAPSHOT_PREFIX,
                                  ScanEntityCache, StopRequested, build_snapshot)
from ui_bot.model import BotSnapshot, BotState


class CommandGateTests(unittest.TestCase):
    def test_toggle_commands_are_consumed_once(self):
        gate = CommandGate()
        gate.submit("resume")
        self.assertTrue(gate.poll_toggle())
        self.assertFalse(gate.poll_toggle())
        gate.submit("resume")
        self.assertFalse(gate.poll_toggle())
        gate.submit("pause")
        self.assertTrue(gate.poll_toggle())

    def test_observed_state_reconciles_without_duplicate_toggle_edges(self):
        gate = CommandGate()
        gate.submit("resume")
        gate.submit("resume")
        self.assertTrue(gate.poll_toggle())
        gate.observe(True)
        self.assertFalse(gate.poll_toggle())
        gate.submit("pause")
        gate.submit("pause")
        self.assertTrue(gate.poll_toggle())

    def test_automatic_memory_wait_does_not_emit_end_toggle(self):
        gate = CommandGate()
        gate.submit("resume")
        self.assertTrue(gate.poll_toggle())
        gate.observe(True)

        gate.submit("memory_wait")
        self.assertFalse(gate.poll_toggle())
        self.assertEqual(gate.poll_internal(), "wait")
        self.assertFalse(gate.poll_toggle())

        gate.submit("memory_recovered")
        self.assertEqual(gate.poll_internal(), "running")
        self.assertFalse(gate.poll_toggle())

    def test_monitor_heartbeat_tracks_loop_poll_not_dashboard_publication(self):
        gate = CommandGate()
        gate._monitor_at = time.monotonic() - 10.0

        gate.poll_toggle()

        self.assertTrue(gate.heartbeat()["monitor_loop_alive"])

    def test_explicit_pause_then_resume_cancels_internal_memory_wait(self):
        gate = CommandGate()
        gate.submit("resume")
        self.assertTrue(gate.poll_toggle())
        gate.observe(True)
        gate.submit("memory_wait")
        self.assertEqual(gate.poll_internal(), "wait")

        gate.submit("pause")
        gate.submit("resume")

        self.assertTrue(gate.poll_toggle())
        gate.observe(True)
        self.assertFalse(gate.poll_toggle())

    def test_explicit_pause_is_reported_during_internal_memory_wait(self):
        gate = CommandGate()
        gate.submit("resume")
        self.assertTrue(gate.poll_toggle())
        gate.observe(True)
        gate.submit("memory_wait")
        self.assertEqual(gate.poll_internal(), "wait")

        gate.submit("pause")

        self.assertEqual(gate.poll_internal(), "pause")
        self.assertFalse(gate.poll_toggle())

    def test_physical_end_can_explicitly_override_wait_when_readiness_recovers(self):
        gate = CommandGate()
        gate.submit("resume")
        self.assertTrue(gate.poll_toggle())
        gate.observe(True)
        gate.submit("memory_wait")
        self.assertEqual(gate.poll_internal(), "wait")
        gate.set_start_readiness(True, "memory", "fresh player read")

        self.assertTrue(gate.allow_hotkey_toggle())
        self.assertIsNone(gate.poll_internal())
        gate.observe(True)
        self.assertFalse(gate.poll_toggle())

    def test_one_physical_end_edge_produces_exactly_one_user_toggle(self):
        gate = CommandGate()
        gate.observe(False)
        gate.set_start_readiness(True, "memory", "ready")
        physical_edges = iter((True, False, True, False))

        def ui_key():
            if gate.poll_toggle():
                return True
            return bool(next(physical_edges) and gate.allow_hotkey_toggle())

        self.assertTrue(ui_key())
        self.assertEqual(gate.physical_toggle_version, 1)
        gate.observe(True)
        self.assertFalse(ui_key())
        self.assertTrue(ui_key())
        self.assertEqual(gate.physical_toggle_version, 2)
        gate.observe(False)
        self.assertFalse(ui_key())

    def test_stop_and_emergency_raise_at_loop_boundary(self):
        for command in ("stop", "emergency"):
            gate = CommandGate()
            gate.submit(command)
            with self.assertRaises(StopRequested):
                gate.poll_toggle()

    def test_invalid_commands_are_logged_not_executed(self):
        events = []
        gate = CommandGate(events.append)
        gate.submit("launch_missiles")
        self.assertFalse(gate.poll_toggle())
        self.assertIn("ignored", events[0])

    def test_controller_configuration_is_replaced_atomically(self):
        gate = CommandGate()
        first = {"buff_slots": [{"id": "one"}], "attack_slots": []}
        second = {"buff_slots": [{"id": "two"}], "attack_slots": []}

        gate.submit("configure", first)
        gate.submit("configure", second)

        self.assertEqual(gate.poll_controller_config(), second)
        self.assertIsNone(gate.poll_controller_config())


class FakeScanner:
    def is_alive(self):
        return True


class FakeArea:
    name = "yard"
    polygon = ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0))
    circles = []
    cells = set()


class FakeEyes:
    def __init__(self):
        self.lock = threading.Lock()
        self.units = [
            ("player", 0x11000, 1.0, 0.0, 2.0),
            ("monster", 0x12000, 5.0, 0.0, 7.0),
            ("monster", 0x13000, 9.0, 0.0, 9.0),
        ]
        self.seen_at = {0x11000: (1.0, 0.0, 2.0),
                        0x12000: (5.0, 0.0, 7.0),
                        0x13000: (9.0, 0.0, 9.0)}
        self.me = 0x11000
        self.owner = 0x11000
        self.chasing = 0x12000
        self.chasing_id = 84
        self.target_name = "Wolf"
        self.fight_ok = {0x12000: (time.time() + 10, True, False),
                         0x13000: (time.time() + 10, False, False)}
        self.ignored = {0x13000: time.time() + 10}
        self.ignored_ids = {85: time.time() + 10}
        self.scan_summary = {"monster_names": {0x12000: "Wolf",
                                                0x13000: "Ghost"}}
        self.area = FakeArea()
        self.path = [(1.0, 2.0), (5.0, 7.0)]
        self.last_pos = (1.0, 0.0, 2.0)
        self.mode = "chasing"
        self.scan_passes = 1
        self.scanner = FakeScanner()

    def _stable_id(self, address):
        return {0x11000: 10, 0x12000: 84, 0x13000: 85}.get(address)


class SnapshotBridgeTests(unittest.TestCase):
    def test_dashboard_state_is_augmented_without_memory_reads(self):
        eyes = FakeEyes()
        info = {"running": True, "source": "memory", "state": "chasing",
                "memory": {"scanner": "READY - REFRESHING"},
                "target": "MONSTER Wolf at 6.4", "navigation": "chasing / direct",
                "warning": ""}

        raw = build_snapshot(info, eyes, sequence=4, trail=[(0.0, 1.0)])
        snapshot = BotSnapshot.from_mapping(raw)

        self.assertEqual(snapshot.state, BotState.RUNNING)
        self.assertEqual(snapshot.player, (1.0, 2.0))
        self.assertEqual(snapshot.target.entity_id, "84")
        self.assertEqual(snapshot.target.name, "Wolf")
        self.assertEqual(snapshot.zone.points[3], (0.0, 20.0))
        self.assertTrue(snapshot.entities[1].current)
        self.assertTrue(snapshot.entities[2].ignored)
        self.assertEqual(snapshot.path[-1], (5.0, 7.0))

    def test_paused_snapshot_never_reports_active_inputs(self):
        raw = build_snapshot({"running": False, "source": "pixels",
                              "state": "paused", "stick": (1.0, 1.0),
                              "attack": True}, FakeEyes(), 1, [])
        self.assertEqual(raw["state"], "PAUSED")
        self.assertEqual(raw["control"], {"stick": [0.0, 0.0], "attack": False})

    def test_scan_cache_uses_scan_rows_stable_ids_liveness_and_logs_counts(self):
        eyes = FakeEyes()
        eyes.units = [
            ("player", 0x11000, 0.0, 0.0, 0.0),
            ("monster", 0x12000, 1.0, 0.0, 1.0),
            ("monster", 0x12100, 2.0, 0.0, 1.0),
            ("monster", 0x13000, 3.0, 0.0, 1.0),
        ]
        eyes.seen_at[0x12000] = (500.0, 0.0, 500.0)
        eyes._stable_id = lambda address: {
            0x11000: 10, 0x12000: 84, 0x12100: 84, 0x13000: 85,
        }.get(address)
        eyes.ms = type("Liveness", (), {
            "monster_target_state": staticmethod(
                lambda _mem, address: (address != 0x13000, False))
        })()
        eyes.mem = object()
        cache = ScanEntityCache(max_entities=20)

        raw = build_snapshot(
            {"running": False, "source": "memory", "state": "paused"},
            eyes, 9, [], scan_cache=cache)
        snapshot = BotSnapshot.from_mapping(raw)

        self.assertEqual(snapshot.scan_version, 1)
        self.assertEqual([item.entity_id for item in snapshot.entities
                          if item.kind == "monster"], ["84", "85"])
        monsters = {item.entity_id: item for item in snapshot.entities
                    if item.kind == "monster"}
        self.assertEqual(monsters["84"].x, 1.0)
        self.assertTrue(monsters["84"].valid_monster)
        self.assertFalse(monsters["85"].valid_monster)
        self.assertEqual(
            raw["log"],
            ["[Scan] total=4 hostile=3 unique=2 inside_zone=1 "
             "valid_targets=1 snapshot=1"])

        eyes.units[1] = ("monster", 0x12000, 40.0, 0.0, 40.0)
        unchanged = BotSnapshot.from_mapping(build_snapshot(
            {"running": False, "source": "memory", "state": "paused"},
            eyes, 10, [], scan_cache=cache))
        self.assertEqual(next(item for item in unchanged.entities
                              if item.entity_id == "84").x, 1.0)
        eyes.scan_passes = 2
        changed = BotSnapshot.from_mapping(build_snapshot(
            {"running": False, "source": "memory", "state": "paused"},
            eyes, 11, [], scan_cache=cache))
        self.assertEqual(next(item for item in changed.entities
                              if item.entity_id == "84").x, 40.0)

    def test_paused_dashboard_starts_read_only_scanner_before_automation(self):
        eyes = FakeEyes()
        eyes.scanner = None
        starts = []

        def start_scanning():
            starts.append("started")
            eyes.scanner = FakeScanner()

        eyes.start_scanning = start_scanning

        class Bot:
            AREA_SAFETY = 5.0

            @staticmethod
            def dashboard_snapshot(_eyes, running, state, *args):
                return {"running": running, "state": state,
                        "source": "memory", "stick": (0.0, 0.0),
                        "attack": False}

        output = io.StringIO()
        dashboard = JsonDashboard(Bot, max_entities=20)
        with redirect_stdout(output):
            dashboard.update(eyes, False, "idle", force=True)

        self.assertEqual(starts, ["started"])
        line = output.getvalue().strip()
        self.assertTrue(line.startswith(SNAPSHOT_PREFIX))
        payload = json.loads(line[len(SNAPSHOT_PREFIX):])
        self.assertEqual(payload["automation_state"], "IDLE")
        self.assertEqual(payload["control"],
                         {"stick": [0.0, 0.0], "attack": False})


if __name__ == "__main__":
    unittest.main()
