import math
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui_bot.app_controller import AppController
from ui_bot.main_window import MainWindow
from ui_bot.model import AutomationState, BotSnapshot, BotState, ConnectionState
from ui_bot.runtime import DemoRuntime
from ui_bot.runtime_child import (JsonDashboard, SNAPSHOT_PREFIX, ScanEntityCache,
                                  build_snapshot)


_APP = QApplication.instance() or QApplication([])


class _Scanner:
    def is_alive(self):
        return True


class _Memory:
    pid = 77


class _Liveness:
    @staticmethod
    def monster_target_state(_mem, _address):
        return True, False


class _Eyes:
    def __init__(self, player=(0.0, 4.0, 12.0)):
        self.lock = threading.Lock()
        self.owner = self.me = 0x11000
        # Deliberately omit the owner row: terminal local_player can remain valid
        # while player-class enumeration is temporarily unavailable.
        self.units = [("monster", 0x12000, 5.0, 0.0, 7.0)]
        self._player = player
        self.chasing = 0x12000
        self.chasing_id = 84
        self.target_name = "Wolf"
        self.area = None
        self.path = [(0.0, 12.0), (5.0, 7.0)]
        self.scan_passes = 1
        self.scan_summary = {"monster_names": {0x12000: "Wolf"}}
        self.scan_error = ""
        self.ignored = {}
        self.ignored_ids = {}
        self.fight_ok = {}
        self.ms = _Liveness()
        self.mem = _Memory()
        self.scanner = _Scanner()

    def _positions(self, addresses):
        if self._player is None or self.owner not in addresses:
            return {}
        return {self.owner: self._player}

    def _stable_id(self, address):
        return {0x11000: 10, 0x12000: 84}.get(address)


class _Bot:
    AREA_SAFETY = 5.0

    @staticmethod
    def dashboard_snapshot(_eyes, running, state, sx, sy, attack, *_args):
        return {
            "running": running,
            "state": state,
            "source": "memory",
            "stick": (sx, sy),
            "attack": attack,
        }


class _Runtime:
    def __init__(self):
        self.calls = []

    def start(self, _options):
        self.calls.append(("start",))

    def resume(self):
        self.calls.append(("resume",))

    def pause(self):
        self.calls.append(("pause",))

    def stop(self):
        self.calls.append(("stop",))

    def emergency_stop(self):
        self.calls.append(("emergency",))

    def reset_emergency(self):
        self.calls.append(("reset",))


def _entity(kind, entity_id, x, z, *, current=False):
    return {
        "id": str(entity_id), "kind": kind, "x": x, "z": z,
        "stable_id": True, "valid_pointer": True, "alive": True,
        "despawned": False, "valid": kind == "monster", "current": current,
    }


def _raw_snapshot(*, sequence=1, player=(0.0, 12.0), player_valid=True,
                  player_fresh=True, running=True, pid=77, session="77:1"):
    return {
        "sequence": sequence,
        "timestamp": 1000.0 + sequence,
        "state": "RUNNING" if running else "PAUSED",
        "connection_state": "CONNECTED",
        "automation_state": "RUNNING" if running else "IDLE",
        "connected": True,
        "memory_session_valid": True,
        "memory_active": True,
        "memory_ready": player_valid and player_fresh,
        "active_mode": "memory" if running else "waiting",
        "source": "memory",
        "player": None if player is None else {"x": player[0], "z": player[1]},
        "player_valid": player_valid,
        "player_fresh": player_fresh,
        "player_error": "" if player_valid else "owner position unavailable",
        "process_id": pid,
        "session_id": session,
        "entities": [
            _entity("player", 10, 99.0, 99.0),
            _entity("monster", 84, 5.0, 7.0, current=True),
        ],
        "target": {
            **_entity("monster", 84, 5.0, 7.0, current=True),
            "distance": 1932.0,
        },
        "path": [[99.0, 99.0], [5.0, 7.0]],
        "trail": [[98.0, 98.0]],
    }


class AdapterPlayerReadTests(unittest.TestCase):
    def test_owner_read_survives_missing_player_row_and_zero_axis(self):
        world, _ = ScanEntityCache().capture(_Eyes(player=(0.0, 4.0, 12.0)), 50.0)

        self.assertEqual(world["player"], [0.0, 12.0])
        self.assertTrue(world["player_valid"])
        self.assertEqual(world["player_error"], "")
        self.assertEqual(len(world["entities"]), 1)

    def test_player_read_failure_keeps_entities_but_neutralizes_snapshot(self):
        eyes = _Eyes(player=None)
        raw = build_snapshot(
            {"running": True, "state": "chasing", "source": "memory",
             "stick": (1.0, -1.0), "attack": True},
            eyes, 4, [(9.0, 9.0)], process_id=77, session_id="77:1")
        snapshot = BotSnapshot.from_mapping(raw)

        self.assertFalse(snapshot.player_valid)
        self.assertFalse(snapshot.memory_active)
        self.assertEqual(snapshot.automation_state, AutomationState.PAUSED)
        self.assertEqual(snapshot.state, BotState.PAUSED)
        self.assertIsNone(snapshot.player)
        self.assertIsNone(snapshot.target)
        self.assertEqual(snapshot.path, ())
        self.assertEqual(snapshot.trail, ())
        self.assertEqual(raw["control"], {"stick": [0.0, 0.0], "attack": False})
        self.assertEqual(len(snapshot.entities), 1)
        self.assertEqual(snapshot.entities[0].kind, "monster")

    def test_protocol_snapshot_has_player_and_connection_diagnostics(self):
        output = io.StringIO()
        dashboard = JsonDashboard(_Bot, expected_pid=77, session_id="77:1")

        with redirect_stdout(output):
            dashboard.update(_Eyes(), True, "chasing", 1.0, 0.0, True, force=True)

        payload = json.loads(output.getvalue().split(SNAPSHOT_PREFIX, 1)[1])
        logs = "\n".join(payload["log"])
        self.assertEqual(payload["process_id"], 77)
        self.assertEqual(payload["session_id"], "77:1")
        self.assertTrue(payload["player_valid"])
        self.assertIn("[PlayerRead] pid=77 raw=", logs)
        self.assertIn("parsed=(0.0, 12.0) valid=True error=", logs)
        self.assertIn("[Snapshot] seq=1 player_valid=True entities=1", logs)
        self.assertIn("[Connection] old=DISCONNECTED new=CONNECTED", logs)


class SnapshotCoherenceTests(unittest.TestCase):
    def test_parser_accepts_zero_axes_using_presence_and_finiteness(self):
        for player in ((0.0, 12.0), (12.0, 0.0)):
            with self.subTest(player=player):
                snapshot = BotSnapshot.from_mapping(
                    _raw_snapshot(player=player, running=False))
                self.assertEqual(snapshot.player, player)
                self.assertTrue(snapshot.player_valid)
                self.assertTrue(all(math.isfinite(value) for value in snapshot.player))

    def test_missing_or_stale_player_sanitizes_target_paths_distance_and_marker(self):
        for changes in (
                {"player": None, "player_valid": False},
                {"player_fresh": False},
                {"player": {"x": None, "z": 2.0}, "player_valid": False}):
            with self.subTest(changes=changes):
                raw = _raw_snapshot()
                raw.update(changes)
                snapshot = BotSnapshot.from_mapping(raw)
                self.assertFalse(snapshot.player_valid)
                self.assertIsNone(snapshot.player)
                self.assertIsNone(snapshot.target)
                self.assertEqual(snapshot.path, ())
                self.assertEqual(snapshot.trail, ())
                self.assertFalse(any(item.kind == "player" for item in snapshot.entities))
                self.assertFalse(any(item.current for item in snapshot.entities))

    def test_disconnected_snapshot_cannot_claim_running_or_active_memory(self):
        raw = _raw_snapshot()
        raw.update(connection_state="DISCONNECTED", connected=False)
        snapshot = BotSnapshot.from_mapping(raw)

        self.assertEqual(snapshot.connection_state, ConnectionState.DISCONNECTED)
        self.assertEqual(snapshot.automation_state, AutomationState.IDLE)
        self.assertFalse(snapshot.memory_active)
        self.assertFalse(snapshot.player_valid)


class ControllerPlayerLossTests(unittest.TestCase):
    def test_player_loss_releases_inputs_then_auto_resumes_after_valid_streak(self):
        runtime = _Runtime()
        logs = []
        controller = AppController(runtime, logs.append,
                                   recovery_valid_snapshots=3)
        controller.start({})
        self.assertTrue(controller.accept_snapshot(
            BotSnapshot.from_mapping(_raw_snapshot(sequence=1))))

        lost = _raw_snapshot(sequence=2, player=None, player_valid=False)
        self.assertTrue(controller.accept_snapshot(BotSnapshot.from_mapping(lost)))

        self.assertEqual(runtime.calls[-1], ("pause",))
        self.assertEqual(controller.automation_state,
                         AutomationState.RECOVERING)
        self.assertEqual(controller.state, BotState.RECOVERING)
        self.assertIsNone(controller.last_snapshot.target)
        self.assertFalse(any(item.kind == "player"
                             for item in controller.last_snapshot.entities))

        for sequence in (3, 4):
            recovered = BotSnapshot.from_mapping(
                _raw_snapshot(sequence=sequence, running=False))
            self.assertTrue(controller.accept_snapshot(recovered))
            self.assertEqual(controller.automation_state,
                             AutomationState.RECOVERING)
            self.assertEqual(runtime.calls.count(("resume",)), 0)

        recovered = BotSnapshot.from_mapping(
            _raw_snapshot(sequence=5, running=False))
        self.assertTrue(controller.accept_snapshot(recovered))
        self.assertEqual(controller.automation_state,
                         AutomationState.RECOVERING)
        self.assertEqual(runtime.calls.count(("resume",)), 1)
        self.assertTrue(controller.desired_running)
        self.assertTrue(any("Recovery" in line and "resuming" in line
                            for line in logs))
        confirmed = BotSnapshot.from_mapping(_raw_snapshot(sequence=6))
        self.assertTrue(controller.accept_snapshot(confirmed))
        self.assertEqual(controller.automation_state,
                         AutomationState.RUNNING)
        self.assertEqual(controller.state, BotState.RUNNING)
        self.assertTrue(any("[Connection]" in line for line in logs))

    def test_invalid_frame_resets_recovery_validation_streak(self):
        runtime = _Runtime()
        controller = AppController(runtime, recovery_valid_snapshots=2)
        controller.start({})
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=1)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=2, player=None, player_valid=False)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=3, running=False)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=4, player=None, player_valid=False,
                          running=False)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=5, running=False)))

        self.assertEqual(runtime.calls.count(("resume",)), 0)
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=6, running=False)))
        self.assertEqual(runtime.calls.count(("resume",)), 1)

    def test_duplicate_sequences_cannot_validate_or_confirm_recovery(self):
        runtime = _Runtime()
        controller = AppController(runtime, recovery_valid_snapshots=3)
        controller.start({"mode": "memory"})
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=1)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=2, player=None, player_valid=False)))

        valid = BotSnapshot.from_mapping(_raw_snapshot(sequence=3, running=False))
        self.assertTrue(controller.accept_snapshot(valid))
        self.assertFalse(controller.accept_snapshot(valid))
        self.assertFalse(controller.accept_snapshot(valid))
        self.assertEqual(runtime.calls.count(("resume",)), 0)

        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=4, running=False)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=5, running=False)))
        self.assertEqual(runtime.calls.count(("resume",)), 1)
        self.assertEqual(controller.state, BotState.RECOVERING)
        self.assertFalse(controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=5))))
        self.assertEqual(controller.state, BotState.RECOVERING)
        self.assertTrue(controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=6))))
        self.assertEqual(controller.state, BotState.RUNNING)

    def test_explicit_pause_cancels_recovery_and_blocks_auto_resume(self):
        runtime = _Runtime()
        controller = AppController(runtime, recovery_valid_snapshots=1)
        controller.start({"mode": "memory"})
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=1)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=2, player=None, player_valid=False)))

        self.assertTrue(controller.pause())
        self.assertFalse(controller.desired_running)
        self.assertEqual(controller.state, BotState.PAUSED)
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=3, running=False)))
        self.assertEqual(runtime.calls.count(("resume",)), 0)

    def test_explicit_stop_cancels_player_loss_recovery(self):
        runtime = _Runtime()
        controller = AppController(runtime, recovery_valid_snapshots=1)
        controller.start({})
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=1)))
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=2, player=None, player_valid=False)))
        self.assertEqual(controller.state, BotState.RECOVERING)

        self.assertTrue(controller.stop())
        controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=3, running=False)))

        self.assertFalse(controller.desired_running)
        self.assertEqual(controller.state, BotState.STOPPED)
        self.assertEqual(runtime.calls.count(("resume",)), 0)

    def test_old_pid_session_snapshot_cannot_replace_current_player(self):
        controller = AppController(_Runtime())
        self.assertTrue(controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=1, pid=77, session="77:1"))))
        self.assertFalse(controller.accept_snapshot(BotSnapshot.from_mapping(
            _raw_snapshot(sequence=2, pid=88, session="77:1",
                          player=(999.0, 999.0)))))
        self.assertEqual(controller.last_snapshot.player, (0.0, 12.0))


class PlayerLossUiTests(unittest.TestCase):
    def test_invalid_player_shows_dist_dash_and_recovery_enables_add_only(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "areas.json").write_text('{"cell":3,"areas":{}}', encoding="utf-8")
        runtime = DemoRuntime()
        window = MainWindow(root, runtime=runtime, demo_mode=True)
        window._test_directory = directory
        window.show()
        QTest.qWait(50)
        page = window.pages["Dashboard"]

        lost = BotSnapshot.from_mapping(_raw_snapshot(
            sequence=50, player=None, player_valid=False))
        window._snapshot_received(lost)
        self.assertFalse(page.add_button.isEnabled())
        self.assertEqual(page.target_card.value.text(), "DIST —")
        self.assertEqual(page.world_view.marker_states.count("target-ring"), 0)

        recovered = BotSnapshot.from_mapping(_raw_snapshot(
            sequence=51, running=False))
        window._snapshot_received(recovered)
        self.assertTrue(page.record_button.isEnabled())
        self.assertNotEqual(window.controller.automation_state,
                            AutomationState.RUNNING)
        self.assertFalse(runtime.running)
        window.emergency_stop("test cleanup")
        window.close()


if __name__ == "__main__":
    unittest.main()
