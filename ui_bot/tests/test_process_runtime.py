import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import unittest

from PySide6.QtCore import QProcess
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from ui_bot.model import BotState
from ui_bot.runtime import ProcessRuntime


_APP = QApplication.instance() or QApplication([])


class ProcessRuntimeIntegrationTests(unittest.TestCase):
    class Finder:
        def __init__(self, *pids):
            self.pids = list(pids)

        def __call__(self):
            return list(self.pids)

    def runtime(self, finder):
        return ProcessRuntime(
            Path(__file__).resolve().parents[2],
            child_module="ui_bot.tests.fake_runtime_child",
            process_finder=finder, retry_min_ms=20, retry_max_ms=40)

    def test_snapshot_heartbeat_restarts_silent_worker_generation(self):
        runtime = ProcessRuntime(
            Path(__file__).resolve().parents[2],
            child_module="ui_bot.tests.fake_runtime_child",
            process_finder=self.Finder(119), retry_min_ms=20, retry_max_ms=40,
            snapshot_stale_ms=80, watchdog_check_ms=20)
        snapshots = QSignalSpy(runtime.signals.snapshot)
        events = QSignalSpy(runtime.signals.event)
        runtime.attach({"mode": "memory"})
        try:
            self.assertTrue(snapshots.wait(3000))
            first_generation = runtime.active_generation
            elapsed = 0
            while elapsed < 3000 and runtime.active_generation == first_generation:
                QTest.qWait(25)
                elapsed += 25
            self.assertGreater(runtime.active_generation, first_generation)
            self.assertTrue(any("heartbeat" in str(events.at(index)[0]).lower()
                                for index in range(events.count())))
        finally:
            runtime.shutdown()

    def test_attach_idle_pause_resume_and_monitor_preserving_stop_protocol(self):
        runtime = self.runtime(self.Finder(111))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        exits = QSignalSpy(runtime.signals.exited)

        runtime.attach({"mode": "memory", "max_entities": 25,
                        "trail_length": 10, "auto_reconnect": True})
        try:
            self.assertTrue(snapshots.wait(3000))
            QTest.qWait(100)
            self.assertTrue(self._has_state(snapshots, BotState.PAUSED))
            self.assertIsNotNone(runtime.process)

            runtime.resume()
            self.assertTrue(self._wait_for_state(snapshots, BotState.RUNNING))
            runtime.pause()
            self.assertTrue(self._wait_for_latest_state(snapshots, BotState.PAUSED))
            runtime.resume()
            self.assertTrue(self._wait_for_latest_state(snapshots, BotState.RUNNING))
            runtime.stop()
            self.assertTrue(self._wait_for_latest_state(snapshots, BotState.PAUSED))
            self.assertIsNotNone(runtime.process)
            self.assertEqual(exits.count(), 0)

            runtime.emergency_stop()
            self.assertTrue(exits.wait(3000))
            self.assertTrue(exits.at(exits.count() - 1)[0])
            self.assertIsNone(runtime.process)
        finally:
            runtime.shutdown()

    def test_ui_opens_while_game_running_and_auto_connects(self):
        runtime = self.runtime(self.Finder(121))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        runtime.attach({"mode": "memory"})
        try:
            self.assertTrue(snapshots.wait(3000))
            latest = snapshots.at(snapshots.count() - 1)[0]
            self.assertEqual(latest.process_id, 121)
            self.assertEqual(latest.session_id, runtime.session_id)
        finally:
            runtime.shutdown()

    def test_start_fallback_restarts_paused_monitor_in_selected_mode(self):
        runtime = self.runtime(self.Finder(131))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        finished = QSignalSpy(runtime.signals.worker_finished)
        runtime.attach({"mode": "memory", "area": "depth2"})
        try:
            self.assertTrue(snapshots.wait(3000))
            old_process = runtime.process
            old_generation = runtime.active_generation
            self.assertIn("--area", old_process.arguments())
            self.assertTrue(runtime.select_mode(
                {"mode": "minimap", "area": "depth2"}))
            self.assertTrue(runtime.switching_mode)
            self.assertIs(runtime.process, old_process)
            self.assertTrue(self._wait_for_latest_state(
                snapshots, BotState.RUNNING, timeout=5000))
            latest = snapshots.at(snapshots.count() - 1)[0]
            self.assertEqual(latest.active_mode, "pixel")
            self.assertEqual(runtime._options["mode"], "minimap")
            self.assertFalse(runtime.switching_mode)
            self.assertIsNot(runtime.process, old_process)
            self.assertEqual(runtime.active_generation, old_generation + 1)
            self.assertNotIn("--area", runtime.process.arguments())
            self.assertNotIn("depth2", runtime.process.arguments())
            self.assertIn("minimap", runtime.process.arguments())
            self.assertGreaterEqual(finished.count(), 1)
            self.assertTrue(finished.at(0)[0].stop_requested)
        finally:
            runtime.shutdown()

    def test_rapid_mode_switches_create_only_one_replacement(self):
        runtime = self.runtime(self.Finder(132))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        runtime.attach({"mode": "memory"})
        try:
            self.assertTrue(snapshots.wait(3000))
            old_generation = runtime.active_generation
            self.assertTrue(runtime.select_mode({"mode": "minimap"}))
            self.assertTrue(runtime.select_mode({"mode": "memory"}))
            self.assertTrue(self._wait_for_latest_state(
                snapshots, BotState.RUNNING, timeout=5000))
            self.assertEqual(runtime.active_generation, old_generation + 1)
            self.assertEqual(runtime._options["mode"], "memory")
            QTest.qWait(150)
            self.assertEqual(runtime.active_generation, old_generation + 1)
        finally:
            runtime.shutdown()

    def test_mode_switch_crash_uses_recovery_not_success_path(self):
        runtime = self.runtime(self.Finder(135))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        finished = QSignalSpy(runtime.signals.worker_finished)
        runtime.attach({"mode": "memory"})
        try:
            self.assertTrue(snapshots.wait(3000))
            generation = runtime.active_generation
            runtime.process.write(b'{"command":"ignore-emergency"}\n')
            runtime.process.waitForBytesWritten(500)
            QTest.qWait(50)
            self.assertTrue(runtime.select_mode({"mode": "minimap"}))
            runtime.process.write(b'{"command":"crash"}\n')
            runtime.process.waitForBytesWritten(500)
            self.assertTrue(finished.wait(3000))
            result = finished.at(0)[0]
            self.assertEqual(result.exit_code, 7)
            self.assertFalse(result.mode_switch)
            self.assertTrue(result.recovery_restart)
            elapsed = 0
            while elapsed < 3000 and runtime.active_generation == generation:
                QTest.qWait(25)
                elapsed += 25
            self.assertGreater(runtime.active_generation, generation)
            self.assertEqual(runtime._options["mode"], "minimap")
            self.assertFalse(runtime._auto_resume)
        finally:
            runtime.shutdown()

    def test_stop_during_hung_switch_starts_one_paused_replacement(self):
        runtime = self.runtime(self.Finder(136))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        runtime.attach({"mode": "memory"})
        try:
            self.assertTrue(snapshots.wait(3000))
            generation = runtime.active_generation
            runtime.process.write(b'{"command":"ignore-emergency"}\n')
            runtime.process.waitForBytesWritten(500)
            QTest.qWait(50)
            self.assertTrue(runtime.select_mode({"mode": "minimap"}))
            runtime.stop()
            elapsed = 0
            while elapsed < 5000 and runtime.active_generation == generation:
                QTest.qWait(25)
                elapsed += 25
            self.assertGreater(runtime.active_generation, generation)
            self.assertEqual(runtime._options["mode"], "minimap")
            self.assertFalse(runtime._auto_resume)
            self.assertTrue(self._wait_for_latest_state(
                snapshots, BotState.PAUSED, timeout=3000))
            QTest.qWait(100)
            self.assertEqual(runtime.active_generation, generation + 1)
        finally:
            runtime.shutdown()

    def test_replaced_area_restarts_same_mode_and_name_worker(self):
        runtime = self.runtime(self.Finder(134))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        runtime.attach({"mode": "memory", "area": "depth2",
                        "area_revision": 1})
        try:
            self.assertTrue(snapshots.wait(3000))
            old_process = runtime.process
            old_generation = runtime.active_generation

            self.assertTrue(runtime.select_mode({
                "mode": "memory", "area": "depth2", "area_revision": 2}))
            self.assertTrue(runtime.switching_mode)
            self.assertIs(runtime.process, old_process)
            self.assertTrue(self._wait_for_latest_state(
                snapshots, BotState.RUNNING, timeout=5000))

            self.assertEqual(runtime.active_generation, old_generation + 1)
            self.assertEqual(runtime._options["area_revision"], 2)
            self.assertIsNot(runtime.process, old_process)
        finally:
            runtime.shutdown()

    def test_old_generation_exit_signal_cannot_stop_replacement(self):
        runtime = self.runtime(self.Finder(133))
        snapshots = QSignalSpy(runtime.signals.snapshot)
        failures = QSignalSpy(runtime.signals.failure)
        runtime.attach({"mode": "memory"})
        try:
            self.assertTrue(snapshots.wait(3000))
            old_generation = runtime.active_generation
            self.assertTrue(runtime.select_mode({"mode": "minimap"}))
            self.assertTrue(self._wait_for_latest_state(
                snapshots, BotState.RUNNING, timeout=5000))
            replacement = runtime.process
            runtime._finished(62097, QProcess.CrashExit,
                              replacement, old_generation)
            self.assertIs(runtime.process, replacement)
            self.assertEqual(runtime.active_generation, old_generation + 1)
            self.assertEqual(failures.count(), 0)
        finally:
            runtime.shutdown()

    def test_ui_before_game_waits_then_auto_connects(self):
        finder = self.Finder()
        runtime = self.runtime(finder)
        snapshots = QSignalSpy(runtime.signals.snapshot)
        runtime.attach({"mode": "memory"})
        try:
            QTest.qWait(50)
            self.assertTrue(runtime.monitoring)
            self.assertIsNone(runtime.process)

            finder.pids = [222]
            runtime.discover_now()

            self.assertTrue(snapshots.wait(3000))
            latest = snapshots.at(snapshots.count() - 1)[0]
            self.assertEqual(latest.process_id, 222)
            self.assertEqual(latest.session_id, runtime.session_id)
        finally:
            runtime.shutdown()

    def test_game_close_and_reopen_connects_to_new_pid_and_session(self):
        finder = self.Finder(301)
        runtime = self.runtime(finder)
        snapshots = QSignalSpy(runtime.signals.snapshot)
        runtime.attach({"mode": "memory"})
        try:
            self.assertTrue(snapshots.wait(3000))
            first = snapshots.at(snapshots.count() - 1)[0]
            first_session = first.session_id
            finder.pids = [302]
            runtime.process.write(b'{"command":"game-close"}\n')

            self.assertTrue(self._wait_for_pid(snapshots, 302, timeout=4000))
            latest = snapshots.at(snapshots.count() - 1)[0]
            self.assertEqual(latest.process_id, 302)
            self.assertNotEqual(latest.session_id, first_session)
        finally:
            runtime.shutdown()

    @staticmethod
    def _has_state(spy, state):
        return any(spy.at(index)[0].state == state
                   for index in range(spy.count()))

    @staticmethod
    def _wait_for_state(spy, state, timeout=2000):
        elapsed = 0
        while elapsed < timeout:
            if ProcessRuntimeIntegrationTests._has_state(spy, state):
                return True
            QTest.qWait(25)
            elapsed += 25
        return False

    @staticmethod
    def _wait_for_latest_state(spy, state, timeout=2000):
        initial = spy.count()
        elapsed = 0
        while elapsed < timeout:
            if (spy.count() > initial and
                    spy.at(spy.count() - 1)[0].state == state):
                return True
            QTest.qWait(25)
            elapsed += 25
        return False

    @staticmethod
    def _wait_for_pid(spy, pid, timeout=2000):
        elapsed = 0
        while elapsed < timeout:
            if any(spy.at(index)[0].process_id == pid
                   for index in range(spy.count())):
                return True
            QTest.qWait(25)
            elapsed += 25
        return False


if __name__ == "__main__":
    unittest.main()
