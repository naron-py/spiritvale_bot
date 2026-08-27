import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

from ui_bot.app_controller import AppController
from ui_bot.config import AtomicConfigStore, UiSettings, application_root
from ui_bot.main_window import MainWindow
from ui_bot.model import (AutomationState, BotSnapshot, BotState, ConnectionState,
                          ZoneDisplayState)
from ui_bot.runtime import (ProcessRuntime, ReconnectBackoff, WorkerDisposition,
                            WorkerExit, WorkerLifetime, WorkerPurpose,
                            classify_worker_exit)
from ui_bot.zone_editor import ZoneDraft, ZoneStore


_APP = QApplication.instance() or QApplication([])


class RuntimePort:
    def __init__(self):
        from ui_bot.runtime import RuntimeSignals
        self.signals = RuntimeSignals()
        self.monitoring = False
        self.running = False
        self.calls = []

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

    def shutdown(self, timeout_ms=0):
        self.monitoring = self.running = False


class WorkerExitClassificationTests(unittest.TestCase):
    def result(self, *, purpose=WorkerPurpose.MONITOR,
               lifetime=WorkerLifetime.PERSISTENT, requested=False,
               code=0, status="NormalExit"):
        return WorkerExit(
            purpose=purpose, expected_lifetime=lifetime,
            stop_requested=requested, exit_code=code, exit_status=status,
            last_valid_snapshot_time=123.0,
        )

    def test_one_shot_normal_zero_is_success_not_safe_stop(self):
        result = self.result(purpose=WorkerPurpose.ONE_SHOT,
                             lifetime=WorkerLifetime.ONE_SHOT)
        self.assertEqual(classify_worker_exit(result),
                         WorkerDisposition.SUCCESS)
        controller = AppController(RuntimePort())
        controller.connection_state = ConnectionState.CONNECTED

        controller.worker_finished(result)

        self.assertEqual(controller.automation_state, AutomationState.IDLE)
        self.assertEqual(controller.connection_state, ConnectionState.CONNECTED)
        self.assertNotEqual(controller.state, BotState.SAFE_STOP)
        self.assertEqual(controller.last_error, "")

    def test_expected_persistent_shutdown_is_normal(self):
        result = self.result(requested=True)
        self.assertEqual(classify_worker_exit(result),
                         WorkerDisposition.NORMAL_SHUTDOWN)
        controller = AppController(RuntimePort())
        controller.worker_finished(result)
        self.assertEqual(controller.connection_state,
                         ConnectionState.DISCONNECTED)
        self.assertEqual(controller.automation_state, AutomationState.IDLE)
        self.assertNotEqual(controller.state, BotState.SAFE_STOP)

    def test_expected_exit_does_not_clear_emergency_safe_stop(self):
        runtime = RuntimePort()
        controller = AppController(runtime)
        controller.emergency_stop("test emergency")
        result = WorkerExit(
            WorkerPurpose.MONITOR, WorkerLifetime.PERSISTENT, True,
            0, "NormalExit")
        controller.worker_finished(result)
        self.assertEqual(controller.automation_state, AutomationState.SAFE_STOP)
        self.assertEqual(controller.state, BotState.EMERGENCY_STOP)

    def test_connecting_monitor_status_is_reflected_by_snapshot(self):
        controller = AppController(RuntimePort())
        controller.monitor_status(ConnectionState.CONNECTING, "Attaching…")
        self.assertEqual(controller.connection_state, ConnectionState.CONNECTING)
        self.assertEqual(controller.last_snapshot.connection_state,
                         ConnectionState.CONNECTING)
        self.assertEqual(controller.last_snapshot.status, "Attaching…")

    def test_unexpected_persistent_normal_exit_requests_restart(self):
        result = self.result()
        self.assertEqual(classify_worker_exit(result),
                         WorkerDisposition.RESTART)

    def test_nonzero_or_crash_is_real_failure(self):
        self.assertEqual(
            classify_worker_exit(self.result(code=1)),
            WorkerDisposition.FAILURE)
        self.assertEqual(
            classify_worker_exit(self.result(status="CrashExit")),
            WorkerDisposition.FAILURE)
        gone = WorkerExit(
            WorkerPurpose.MONITOR, WorkerLifetime.PERSISTENT, False,
            1, "CrashExit", process_gone=True)
        self.assertEqual(classify_worker_exit(gone), WorkerDisposition.FAILURE)

    def test_reconnect_backoff_is_bounded_and_resettable(self):
        backoff = ReconnectBackoff(initial_ms=100, maximum_ms=400)
        self.assertEqual([backoff.next_delay() for _ in range(5)],
                         [100, 200, 400, 400, 400])
        backoff.reset()
        self.assertEqual(backoff.next_delay(), 100)


class CurrentSessionSnapshotTests(unittest.TestCase):
    @staticmethod
    def snapshot(pid, session, sequence=1):
        return BotSnapshot.from_mapping({
            "sequence": sequence, "timestamp": float(sequence),
            "state": "PAUSED", "connection_state": "CONNECTED",
            "automation_state": "IDLE", "connected": True,
            "memory_active": True, "player_fresh": True,
            "player": {"x": 4.0, "z": 8.0},
            "process_id": pid, "session_id": session,
        })

    def test_old_pid_and_session_snapshots_are_rejected(self):
        runtime = ProcessRuntime(Path(__file__).resolve().parents[2],
                                 process_finder=lambda: [])
        runtime.current_pid = 202
        runtime.session_id = "202:2"

        self.assertFalse(runtime.snapshot_belongs_to_current_session(
            self.snapshot(101, "101:1")))
        self.assertFalse(runtime.snapshot_belongs_to_current_session(
            self.snapshot(202, "202:1")))
        self.assertTrue(runtime.snapshot_belongs_to_current_session(
            self.snapshot(202, "202:2")))


class SavedZoneStartupTests(unittest.TestCase):
    def make_root(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "ui_bot" / "state").mkdir(parents=True)
        return directory, root

    def save_zone(self, root, name="depth2"):
        store = ZoneStore(root / "areas.json")
        store.save_polygon(name, [(0, 0), (10, 0), (10, 10), (0, 10)],
                           select=True)
        AtomicConfigStore(root / "ui_bot" / "state" / "settings.json").save(
            UiSettings(selected_area=name))

    def test_saved_zone_loads_before_connection_and_remains_visible(self):
        directory, root = self.make_root()
        self.addCleanup(directory.cleanup)
        self.save_zone(root)
        runtime = RuntimePort()

        window = MainWindow(root, runtime=runtime)
        self.addCleanup(window.close)

        self.assertTrue(window.latest_snapshot.zone.valid)
        self.assertEqual(window.latest_snapshot.zone.name, "depth2")
        self.assertEqual(window.latest_snapshot.zone_display_state,
                         ZoneDisplayState.LOADED_DISCONNECTED)
        self.assertEqual(window.pages["Dashboard"].zone_status.text(),
                         "ZONE LOADED, GAME DISCONNECTED")
        self.assertTrue(window.pages["Dashboard"].zone_status.wordWrap())
        self.assertEqual(window.pages["Dashboard"].zone_status.toolTip(),
                         "ZONE LOADED, GAME DISCONNECTED")
        self.assertFalse(window.start_button.isEnabled())
        self.assertFalse(window.pause_button.isEnabled())
        self.assertFalse(window.stop_button.isEnabled())
        self.assertIn("QPushButton#start:disabled", window.styleSheet())
        dashboard = window.pages["Dashboard"]
        self.assertFalse(dashboard.record_button.isEnabled())
        self.assertFalse(dashboard.add_button.isEnabled())
        self.assertTrue(window.pages["Settings"].isEnabled())

    def test_saved_zone_selection_and_coordinates_survive_restart(self):
        directory, root = self.make_root()
        self.addCleanup(directory.cleanup)
        store = ZoneStore(root / "areas.json")
        store.save_polygon("restart-zone",
                           [(1, 1), (8, 1), (8, 7), (1, 7)], select=True)
        selected = store.load_selected("")

        reopened = ZoneStore(root / "areas.json")
        loaded = reopened.load_selected("")

        self.assertEqual(selected, loaded)
        self.assertEqual(loaded.name, "restart-zone")
        self.assertEqual(loaded.points,
                         ((1.0, 1.0), (8.0, 1.0), (8.0, 7.0), (1.0, 7.0)))

    def test_replacing_selected_polygon_changes_worker_area_revision(self):
        directory, root = self.make_root()
        self.addCleanup(directory.cleanup)
        self.save_zone(root, "depth2")
        window = MainWindow(root, runtime=RuntimePort())
        self.addCleanup(window.close)
        before = window._runtime_options("memory")

        window.draft = ZoneDraft(
            "depth2", [(20, 20), (40, 20), (40, 40), (20, 40)])
        window._recording = True
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            window.finish_zone()
        after = window._runtime_options("memory")

        self.assertEqual(before["area"], "depth2")
        self.assertEqual(after["area"], "depth2")
        self.assertNotEqual(before["area_revision"],
                            after["area_revision"])

    def test_terminal_shape_points_polygon_loads_after_reopen(self):
        directory, root = self.make_root()
        self.addCleanup(directory.cleanup)
        payload = {
            "cell": 3.0,
            "selected_area": "depth2",
            "areas": {"depth2": {
                "shape": "polygon", "axes": "xz",
                "points": [[0, 0], [12, 0], [12, 8], [0, 8]],
            }},
        }
        (root / "areas.json").write_text(
            __import__("json").dumps(payload), encoding="utf-8")
        loaded = ZoneStore(root / "areas.json").load_selected("")
        self.assertTrue(loaded.valid)
        self.assertEqual(loaded.name, "depth2")
        self.assertEqual(len(loaded.points), 4)

    def test_atomic_zone_selection_drives_display_and_worker_launch(self):
        directory, root = self.make_root()
        self.addCleanup(directory.cleanup)
        store = ZoneStore(root / "areas.json")
        store.save_polygon("old", [(0, 0), (5, 0), (5, 5)], select=True)
        store.save_polygon("new", [(10, 10), (15, 10), (15, 15)], select=True)
        AtomicConfigStore(root / "ui_bot" / "state" / "settings.json").save(
            UiSettings(selected_area="old"))
        runtime = RuntimePort()
        window = MainWindow(root, runtime=runtime)
        self.addCleanup(window.close)
        self.assertEqual(window.saved_zone.name, "new")
        self.assertEqual(window.settings_value.selected_area, "new")
        self.assertEqual(runtime.calls[0][1]["area"], "new")

    def test_application_root_does_not_depend_on_current_directory(self):
        expected = application_root()
        with tempfile.TemporaryDirectory() as directory:
            previous = Path.cwd()
            try:
                os.chdir(directory)
                self.assertEqual(application_root(), expected)
            finally:
                os.chdir(previous)

    def test_finish_requires_three_unique_points(self):
        directory, root = self.make_root()
        self.addCleanup(directory.cleanup)
        window = MainWindow(root, runtime=RuntimePort())
        self.addCleanup(window.close)
        page = window.pages["Dashboard"]
        window._recording = True
        window.draft.points = [(0.0, 0.0), (1.0, 0.0), (0.0, 0.0)]
        window._refresh_recorder_controls()
        self.assertFalse(page.save_button.isEnabled())
        window.draft.points.append((0.0, 1.0))
        window._refresh_recorder_controls()
        self.assertTrue(page.save_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
