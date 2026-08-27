import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui_bot.app_controller import AppController
from ui_bot.main_window import MainWindow
from ui_bot.model import (AutomationState, BotSnapshot, ConnectionState,
                          ZoneRecordingState)
from ui_bot.pages import DashboardPage
from ui_bot.runtime import DemoRuntime
from ui_bot.widgets.world_view import WorldView


_APP = QApplication.instance() or QApplication([])


def circle_snapshot(entities, sequence=1, zone=True):
    raw = {
        "sequence": sequence,
        "timestamp": float(sequence),
        "state": "PAUSED",
        "connected": True,
        "memory_active": True,
        "source": "memory",
        "player": {"x": 0.0, "z": 0.0},
        "player_fresh": True,
        "scan_version": sequence,
        "entities": entities,
    }
    if zone:
        raw["zone"] = {
            "name": "farm-circle",
            "kind": "circles",
            "circles": [[0.0, 0.0, 10.0]],
        }
    return BotSnapshot.from_mapping(raw)


def monster(entity_id, x, z, *, alive=True, despawned=False,
            valid_pointer=True, current=False, name="Slime"):
    return {
        "id": str(entity_id),
        "stable_id": True,
        "kind": "monster",
        "x": float(x),
        "z": float(z),
        "name": name,
        "alive": alive,
        "despawned": despawned,
        "valid_pointer": valid_pointer,
        "valid": alive and not despawned and valid_pointer,
        "current": current,
    }


class CanonicalZoneEntityTests(unittest.TestCase):
    def test_five_valid_monsters_inside_match_counter_and_green_markers(self):
        snapshot = circle_snapshot([
            monster(100 + index, index + 1, 0, current=index == 0)
            for index in range(5)
        ])
        page = DashboardPage()
        view = WorldView()

        page.apply_snapshot(snapshot)
        view.update_snapshot(snapshot)

        self.assertEqual(len(snapshot.monsters_in_zone), 5)
        self.assertEqual(page.zone_card.value.text(), "5 MONSTERS")
        self.assertEqual(view.marker_states.count("green"), 5)
        self.assertEqual(view.marker_states.count("target-ring"), 1)
        page.close()
        view.close()

    def test_duplicate_records_are_counted_once_by_stable_id(self):
        snapshot = circle_snapshot([
            monster(51, 2, 0, name="Same Name"),
            monster(51, 2, 0, name="Same Name"),
            monster(52, 3, 0, name="Same Name"),
        ])

        self.assertEqual([item.entity_id for item in snapshot.entities], ["51", "52"])
        self.assertEqual(len(snapshot.monsters_in_zone), 2)

    def test_move_outside_changes_marker_and_count_in_same_snapshot(self):
        inside = circle_snapshot([monster(1, 9.9, 0)], sequence=4)
        outside = circle_snapshot([monster(1, 10.1, 0)], sequence=5)
        view = WorldView()

        view.update_snapshot(inside)
        self.assertEqual(len(inside.monsters_in_zone), 1)
        self.assertEqual(view.marker_states, ("green",))
        view.update_snapshot(outside)
        self.assertEqual(len(outside.monsters_in_zone), 0)
        self.assertEqual(view.marker_states, ("red",))
        view.close()

    def test_circle_boundary_is_inside(self):
        snapshot = circle_snapshot([monster(7, 10.0, 0)])
        self.assertEqual(len(snapshot.monsters_in_zone), 1)

    def test_dead_despawned_invalid_and_non_monsters_are_never_counted(self):
        rows = [
            monster(1, 1, 0, alive=False),
            monster(2, 2, 0, despawned=True),
            monster(3, 3, 0, valid_pointer=False),
            {"id": "4", "stable_id": True, "kind": "npc", "x": 4, "z": 0,
             "alive": True, "valid_pointer": True, "valid": True},
            {"id": "player", "stable_id": True, "kind": "player", "x": 0, "z": 0,
             "alive": True, "valid_pointer": True, "valid": True},
        ]
        snapshot = circle_snapshot(rows)

        self.assertEqual(snapshot.monsters_in_zone, ())
        self.assertTrue(all(not item.valid_monster for item in snapshot.entities))

    def test_no_valid_zone_displays_dash(self):
        snapshot = circle_snapshot([monster(1, 1, 0)], zone=False)
        page = DashboardPage()

        page.apply_snapshot(snapshot)

        self.assertFalse(snapshot.zone.valid)
        self.assertEqual(page.zone_card.value.text(), "—")
        page.close()


class MonitoringRuntime:
    def __init__(self):
        self.calls = []
        self.monitoring = False
        self.running = False

    def attach(self, options):
        self.calls.append(("attach", dict(options)))
        self.monitoring = True

    def resume(self):
        self.calls.append(("resume",))
        self.running = True

    def pause(self):
        self.calls.append(("pause",))
        self.running = False

    def stop(self):
        self.calls.append(("stop",))
        self.running = False

    def emergency_stop(self):
        self.calls.append(("emergency",))
        self.running = self.monitoring = False

    def reset_emergency(self):
        self.calls.append(("reset",))


class IndependentMonitoringTests(unittest.TestCase):
    def test_attach_without_start_keeps_automation_idle(self):
        runtime = MonitoringRuntime()
        controller = AppController(runtime)

        self.assertTrue(controller.attach({"mode": "memory"}))
        self.assertEqual(controller.connection_state, ConnectionState.CONNECTING)
        self.assertEqual(controller.automation_state, AutomationState.IDLE)
        self.assertEqual(runtime.calls, [("attach", {"mode": "memory"})])

        controller.accept_snapshot(circle_snapshot([], sequence=2))
        self.assertEqual(controller.connection_state, ConnectionState.CONNECTED)
        self.assertEqual(controller.automation_state, AutomationState.IDLE)

    def test_stop_releases_automation_but_preserves_monitor(self):
        runtime = MonitoringRuntime()
        controller = AppController(runtime)
        controller.attach({})
        controller.accept_snapshot(circle_snapshot([], sequence=2))
        controller.start({})

        self.assertTrue(controller.stop())
        self.assertEqual(controller.automation_state, AutomationState.IDLE)
        self.assertTrue(runtime.monitoring)
        self.assertFalse(runtime.running)
        self.assertEqual(runtime.calls[-1], ("stop",))


class ZoneRecordingWhileIdleTests(unittest.TestCase):
    def make_window(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "areas.json").write_text(
            '{"cell":3,"areas":{}}', encoding="utf-8")
        runtime = DemoRuntime()
        window = MainWindow(root, runtime=runtime, demo_mode=True)
        window._test_directory = directory
        window.show()
        QTest.qWait(100)
        return window, runtime

    def test_attach_without_start_updates_player_and_records_position(self):
        window, runtime = self.make_window()
        self.assertTrue(runtime.monitoring)
        self.assertFalse(runtime.running)
        self.assertTrue(window.latest_snapshot.player_fresh)

        page = window.pages["Farming Zone"]
        page.zone_name.setText("idle-zone")
        window.show_page("Farming Zone")
        window.start_recording()
        window.add_position()

        self.assertEqual(window.zone_recording_state,
                         ZoneRecordingState.RECORDING)
        self.assertEqual(len(window.draft.points), 1)
        self.assertFalse(runtime.running)
        window.emergency_stop("test cleanup")
        window.close()

    def test_stop_while_recording_keeps_scan_and_draft(self):
        window, runtime = self.make_window()
        page = window.pages["Farming Zone"]
        page.zone_name.setText("stop-zone")
        window.show_page("Farming Zone")
        window.start_recording()
        window.add_position()
        window.start_bot()
        QTest.qWait(20)

        window.controller.stop()
        window._refresh_controls()

        self.assertTrue(runtime.monitoring)
        self.assertFalse(runtime.running)
        self.assertEqual(len(window.draft.points), 1)
        self.assertEqual(window.zone_recording_state,
                         ZoneRecordingState.RECORDING)
        window.emergency_stop("test cleanup")
        window.close()

    def test_disconnect_while_recording_preserves_points_and_shows_error(self):
        window, _ = self.make_window()
        page = window.pages["Farming Zone"]
        page.zone_name.setText("disconnect-zone")
        window.show_page("Farming Zone")
        window.start_recording()
        window.add_position()
        before = tuple(window.draft.points)

        disconnected = BotSnapshot.from_mapping({
            "sequence": window.latest_snapshot.sequence + 1,
            "timestamp": 999.0,
            "state": "DISCONNECTED",
            "connection_state": "DISCONNECTED",
            "automation_state": "IDLE",
            "connected": False,
            "memory_active": False,
            "source": "memory",
            "player": None,
            "player_fresh": False,
        })
        window.apply_snapshot(disconnected)

        self.assertEqual(tuple(window.draft.points), before)
        self.assertEqual(window.zone_recording_state,
                         ZoneRecordingState.RECORDING)
        self.assertFalse(page.add_button.isEnabled())
        self.assertIn("valid player position unavailable",
                      window.recorder_message.lower())
        window.emergency_stop("test cleanup")
        window.close()

    def test_connected_but_stale_player_disables_add_with_exact_error(self):
        window, _ = self.make_window()
        page = window.pages["Farming Zone"]
        page.zone_name.setText("stale-zone")
        window.show_page("Farming Zone")
        window.start_recording()
        window.add_position()
        before = tuple(window.draft.points)
        raw = dict(window.latest_snapshot.raw)
        raw.update(sequence=window.latest_snapshot.sequence + 1,
                   connection_state="CONNECTED", connected=True,
                   memory_active=True, player_fresh=False)
        window.apply_snapshot(BotSnapshot.from_mapping(raw))

        self.assertFalse(page.add_button.isEnabled())
        window.add_position()
        self.assertEqual(tuple(window.draft.points), before)
        self.assertEqual(
            window.recorder_message,
            "Cannot add point: valid player position unavailable.")
        window.emergency_stop("test cleanup")
        window.close()


if __name__ == "__main__":
    unittest.main()
