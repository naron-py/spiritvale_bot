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

    def wait_for_memory(self):
        self.calls.append(("memory_wait",))

    def memory_recovered(self):
        self.calls.append(("memory_recovered",))

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
                  player_fresh=True, running=True, pid=77, session="77:1",
                  scan_version=None, player_read_version=None,
                  scan_in_progress=False, scanner_alive=True,
                  scan_timed_out=False, physical_toggle_version=0):
    scan_version = sequence if scan_version is None else scan_version
    player_read_version = (sequence if player_read_version is None
                           else player_read_version)
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
        "scan_version": scan_version,
        "player_read_version": player_read_version,
        "scan_in_progress": scan_in_progress,
        "scanner_alive": scanner_alive,
        "scan_timed_out": scan_timed_out,
        "physical_toggle_version": physical_toggle_version,
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

    def test_player_read_refreshes_while_entity_scan_generation_is_unchanged(self):
        eyes = _Eyes(player=(1.0, 0.0, 2.0))
        cache = ScanEntityCache()
        first, _ = cache.capture(eyes, 100.0)
        eyes._player = (4.0, 0.0, 8.0)

        second, changed = cache.capture(eyes, 112.0)

        self.assertFalse(changed)
        self.assertEqual(second["version"], first["version"])
        self.assertGreater(second["player_read_version"],
                           first["player_read_version"])
        self.assertEqual(second["player"], [4.0, 8.0])
        self.assertEqual(second["captured_at"], first["captured_at"])
        raw = build_snapshot(
            {"running": True, "state": "chasing", "source": "memory",
             "stick": (1.0, 0.0), "attack": True},
            eyes, 2, [], scan_world=second, now=112.0,
            process_id=77, session_id="77:1")
        self.assertTrue(raw["memory_ready"])
        self.assertFalse(raw["scan_timed_out"])

    def test_active_scan_past_bounded_timeout_is_reported_separately(self):
        eyes = _Eyes(player=(1.0, 0.0, 2.0))
        eyes.scan_in_progress = True
        eyes.scan_started_at = 100.0
        eyes.last_scan_completed_at = 50.0
        world, _ = ScanEntityCache().capture(eyes, 131.0)

        raw = build_snapshot(
            {"running": True, "state": "chasing", "source": "memory",
             "stick": (1.0, 0.0), "attack": True},
            eyes, 2, [], scan_world=world, now=131.0,
            process_id=77, session_id="77:1")

        self.assertTrue(raw["player_valid"])
        self.assertTrue(raw["player_fresh"])
        self.assertTrue(raw["scan_in_progress"])
        self.assertTrue(raw["scan_timed_out"])
        self.assertFalse(raw["memory_ready"])

    def test_timeout_uses_current_scan_start_not_previous_completion(self):
        eyes = _Eyes(player=(1.0, 0.0, 2.0))
        eyes.scan_in_progress = True
        eyes.scan_started_at = 100.0
        eyes.last_scan_completed_at = 50.0
        world, _ = ScanEntityCache().capture(eyes, 125.0)

        raw = build_snapshot(
            {"running": True, "state": "chasing", "source": "memory",
             "stick": (1.0, 0.0), "attack": True},
            eyes, 2, [], scan_world=world, now=125.0,
            process_id=77, session_id="77:1")

        self.assertFalse(raw["scan_timed_out"])

    def test_player_read_retries_if_memory_generation_changes_mid_read(self):
        class RacingEyes(_Eyes):
            def __init__(self):
                super().__init__(player=(1.0, 0.0, 2.0))
                self.generation = 1
                self.scan_version = 3
                self.me = 0x1000
                self.owner = self.me
                self._raced = False

            def _positions(self, owners):
                owner = owners[0]
                if not self._raced:
                    self._raced = True
                    self.generation = 2
                    self.scan_version = 4
                    self.me = 0x2000
                    self.owner = self.me
                    return {owner: (1.0, 0.0, 2.0)}
                return {owner: (9.0, 0.0, 8.0)}

        world, _ = ScanEntityCache().capture(RacingEyes(), 100.0)

        self.assertEqual(world["version"], 4)
        self.assertEqual(world["player"], [9.0, 8.0])

    def test_scan_version_remains_monotonic_when_session_pass_count_resets(self):
        eyes = _Eyes(player=(1.0, 0.0, 2.0))
        eyes.scan_version = 8
        cache = ScanEntityCache()
        first, _ = cache.capture(eyes, 100.0)

        eyes.scan_passes = 0
        eyes.scan_version = 9
        second, changed = cache.capture(eyes, 101.0)

        self.assertEqual(first["version"], 8)
        self.assertEqual(second["version"], 9)
        self.assertTrue(changed)

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
    def test_physical_end_start_and_stop_are_parent_authoritative_once(self):
        runtime = _Runtime()
        controller = AppController(runtime)
        controller.attach({"mode": "memory"})

        controller.accept_snapshot(_raw_snapshot(
            sequence=1, running=True, physical_toggle_version=1))
        self.assertTrue(controller.desired_running)
        self.assertEqual(controller.state, BotState.RUNNING)
        self.assertEqual(runtime.calls.count(("pause",)), 0)

        controller.accept_snapshot(_raw_snapshot(
            sequence=2, running=False, physical_toggle_version=2))
        self.assertFalse(controller.desired_running)
        self.assertEqual(controller.state, BotState.PAUSED)
        self.assertEqual(runtime.calls.count(("pause",)), 0)

    def test_twelve_second_entity_scan_does_not_pause_with_fresh_player_reads(self):
        runtime = _Runtime()
        controller = AppController(runtime, progress_timeout_s=60.0)
        controller.start({"mode": "memory"})

        for sequence in range(1, 14):
            raw = _raw_snapshot(
                sequence=sequence, scan_version=1,
                player_read_version=sequence, scan_in_progress=True)
            self.assertTrue(controller.accept_snapshot(raw))

        self.assertEqual(runtime.calls.count(("pause",)), 0)
        self.assertEqual(runtime.calls.count(("memory_wait",)), 0)
        self.assertFalse(any(call[0] == "restart_current" for call in runtime.calls))
        self.assertEqual(controller.state, BotState.RUNNING)

    def test_one_real_player_read_miss_does_not_pause_and_next_read_recovers(self):
        runtime = _Runtime()
        controller = AppController(runtime)
        controller.start({"mode": "memory"})
        controller.accept_snapshot(_raw_snapshot(
            sequence=1, scan_version=5, player_read_version=10))

        controller.accept_snapshot(_raw_snapshot(
            sequence=2, scan_version=5, player_read_version=11,
            player=None, player_valid=False, player_fresh=False))
        self.assertEqual(controller.state, BotState.RUNNING)
        self.assertEqual(controller.automation_state, AutomationState.RUNNING)
        controller.accept_snapshot(_raw_snapshot(
            sequence=3, scan_version=5, player_read_version=12))

        self.assertEqual(runtime.calls.count(("pause",)), 0)
        self.assertEqual(runtime.calls.count(("memory_wait",)), 0)
        self.assertEqual(runtime.calls.count(("memory_recovered",)), 0)
        self.assertEqual(controller.state, BotState.RUNNING)

    def test_scan_timeout_waits_internally_then_one_new_scan_resumes_once(self):
        runtime = _Runtime()
        logs = []
        controller = AppController(runtime, logs.append)
        controller.start({"mode": "memory"})
        controller.accept_snapshot(_raw_snapshot(
            sequence=1, scan_version=7, player_read_version=20))

        controller.accept_snapshot(_raw_snapshot(
            sequence=2, scan_version=7, player_read_version=21,
            running=False, scan_in_progress=True, scan_timed_out=True))
        self.assertEqual(runtime.calls.count(("memory_wait",)), 1)
        self.assertEqual(runtime.calls.count(("pause",)), 0)

        for sequence in (3, 4, 5):
            controller.accept_snapshot(_raw_snapshot(
                sequence=sequence, scan_version=7,
                player_read_version=20 + sequence, running=False,
                scan_in_progress=True, scan_timed_out=False))
        self.assertEqual(runtime.calls.count(("memory_recovered",)), 0)

        controller.accept_snapshot(_raw_snapshot(
            sequence=6, scan_version=8, player_read_version=26,
            running=False))
        controller.accept_snapshot(_raw_snapshot(
            sequence=7, scan_version=8, player_read_version=27,
            running=False))

        self.assertEqual(runtime.calls.count(("memory_recovered",)), 1)
        self.assertEqual(runtime.calls.count(("resume",)), 0)
        self.assertTrue(any("WAITING: memory scan delayed" in line for line in logs))
        self.assertTrue(any("RUNNING: fresh player read received" in line
                            for line in logs))

    def test_dead_scanner_waits_safely_without_worker_restart(self):
        runtime = _Runtime()
        controller = AppController(runtime, recovery_restart_s=1.0,
                                   clock=lambda: 100.0)
        controller.start({"mode": "memory"})
        controller.accept_snapshot(_raw_snapshot(sequence=1, scan_version=2))
        controller.accept_snapshot(_raw_snapshot(
            sequence=2, scan_version=2, player_read_version=2,
            running=False, scanner_alive=False))

        controller.tick_recovery()
        self.assertEqual(runtime.calls.count(("memory_wait",)), 1)
        self.assertFalse(any(call[0] == "restart_current" for call in runtime.calls))

    def test_memory_without_ready_pixels_waits_on_scan_timeout(self):
        memory_runtime = _Runtime()
        memory = AppController(memory_runtime)
        memory.start({"mode": "memory"})
        memory.accept_snapshot(_raw_snapshot(
            sequence=1, scan_version=2, player_read_version=1))
        delayed = _raw_snapshot(
            sequence=2, scan_version=2, player_read_version=2,
            running=True, scan_in_progress=True, scan_timed_out=True)
        delayed["active_mode"] = delayed["source"] = "pixels"
        delayed["pixel_ready"] = False
        memory.accept_snapshot(delayed)
        self.assertEqual(memory_runtime.calls.count(("memory_wait",)), 1)

        pixel_runtime = _Runtime()
        pixel = AppController(pixel_runtime)
        pixel.start({"mode": "pixel"})
        pixel.accept_snapshot(dict(delayed, sequence=1))
        self.assertEqual(pixel_runtime.calls.count(("memory_wait",)), 0)

    def test_three_real_player_misses_wait_then_one_fresh_read_resumes(self):
        runtime = _Runtime()
        logs = []
        controller = AppController(runtime, logs.append)
        controller.start({})
        self.assertTrue(controller.accept_snapshot(
            BotSnapshot.from_mapping(_raw_snapshot(sequence=1))))

        for sequence in (2, 3, 4):
            lost = _raw_snapshot(
                sequence=sequence, scan_version=1,
                player_read_version=sequence, player=None,
                player_valid=False, player_fresh=False)
            self.assertTrue(controller.accept_snapshot(lost))

        self.assertEqual(runtime.calls[-1], ("memory_wait",))
        self.assertEqual(controller.automation_state,
                         AutomationState.RECOVERING)
        self.assertEqual(controller.state, BotState.RECOVERING)
        self.assertIsNone(controller.last_snapshot.target)
        self.assertFalse(any(item.kind == "player"
                             for item in controller.last_snapshot.entities))

        recovered = BotSnapshot.from_mapping(_raw_snapshot(
            sequence=5, scan_version=1, player_read_version=5,
            running=False))
        self.assertTrue(controller.accept_snapshot(recovered))
        self.assertEqual(controller.automation_state,
                         AutomationState.RECOVERING)
        self.assertEqual(runtime.calls.count(("memory_recovered",)), 1)
        self.assertTrue(controller.desired_running)
        self.assertTrue(any("RUNNING: fresh player read received" in line
                            for line in logs))
        confirmed = BotSnapshot.from_mapping(_raw_snapshot(sequence=6))
        self.assertTrue(controller.accept_snapshot(confirmed))
        self.assertEqual(controller.automation_state,
                         AutomationState.RUNNING)
        self.assertEqual(controller.state, BotState.RUNNING)
        self.assertTrue(any("[Connection]" in line for line in logs))

    def test_player_read_recovery_never_restarts_live_worker_and_logs_downtime(self):
        now = [100.0]
        runtime = _Runtime()
        logs = []
        controller = AppController(runtime, logs.append,
                                   recovery_restart_s=5.0,
                                   clock=lambda: now[0])
        controller.start({"mode": "memory"})
        controller.accept_snapshot(_raw_snapshot(
            sequence=1, player_read_version=1))
        for sequence in (2, 3, 4):
            controller.accept_snapshot(_raw_snapshot(
                sequence=sequence, scan_version=1,
                player_read_version=sequence, player=None,
                player_valid=False, player_fresh=False))

        now[0] += 12.0
        controller.tick_recovery()
        self.assertFalse(any(call[0] == "restart_current"
                             for call in runtime.calls))
        controller.accept_snapshot(_raw_snapshot(
            sequence=5, scan_version=1, player_read_version=5,
            running=False))
        controller.accept_snapshot(_raw_snapshot(
            sequence=6, scan_version=1, player_read_version=6))
        log = "\n".join(logs)
        self.assertIn("[Downtime] start reason=", log)
        self.assertIn("[Downtime] end reason=", log)
        self.assertIn("duration_ms=12000", log)

    def test_memory_mode_uses_ready_pixel_fallback_without_memory_wait(self):
        runtime = _Runtime()
        logs = []
        controller = AppController(runtime, logs.append)
        controller.start({"mode": "memory"})
        raw = _raw_snapshot(
            sequence=1, player_read_version=1,
            player=None, player_valid=False, player_fresh=False)
        raw.update(active_mode="pixel", source="pixels", pixel_ready=True,
                   memory_ready=False, scan_in_progress=True,
                   scan_timed_out=True, scanner_alive=False)

        controller.accept_snapshot(raw)

        self.assertEqual(controller.state, BotState.RUNNING)
        self.assertNotIn(("memory_wait",), runtime.calls)
        self.assertTrue(any("action=fallback" in line for line in logs))

    def test_republished_failed_read_does_not_count_as_new_failure(self):
        runtime = _Runtime()
        controller = AppController(runtime)
        controller.start({})
        controller.accept_snapshot(_raw_snapshot(
            sequence=1, player_read_version=1))
        for sequence in (2, 3, 4):
            controller.accept_snapshot(_raw_snapshot(
                sequence=sequence, scan_version=1, player_read_version=2,
                player=None, player_valid=False, player_fresh=False))

        self.assertEqual(runtime.calls.count(("memory_wait",)), 0)
        controller.accept_snapshot(_raw_snapshot(
            sequence=5, scan_version=1, player_read_version=3,
            player=None, player_valid=False, player_fresh=False))
        controller.accept_snapshot(_raw_snapshot(
            sequence=6, scan_version=1, player_read_version=4,
            player=None, player_valid=False, player_fresh=False))
        self.assertEqual(runtime.calls.count(("memory_wait",)), 1)

    def test_same_scan_generation_cannot_resume_scan_delay_recovery(self):
        runtime = _Runtime()
        controller = AppController(runtime)
        controller.start({"mode": "memory"})
        controller.accept_snapshot(_raw_snapshot(
            sequence=1, scan_version=4, player_read_version=1))
        controller.accept_snapshot(_raw_snapshot(
            sequence=2, scan_version=4, player_read_version=2,
            running=False, scan_in_progress=True, scan_timed_out=True))

        for sequence in (3, 4, 5):
            controller.accept_snapshot(_raw_snapshot(
                sequence=sequence, scan_version=4,
                player_read_version=sequence, running=False))
        self.assertEqual(runtime.calls.count(("memory_recovered",)), 0)

        controller.accept_snapshot(_raw_snapshot(
            sequence=6, scan_version=5, player_read_version=6,
            running=False))
        self.assertEqual(runtime.calls.count(("memory_recovered",)), 1)

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
            _raw_snapshot(sequence=2, running=False,
                          scan_in_progress=True, scan_timed_out=True)))
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
