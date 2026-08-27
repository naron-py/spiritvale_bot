import io
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui_bot.main_window import MainWindow
from ui_bot.model import AutomationState, BotSnapshot, BotState
from ui_bot.readiness import evaluate_start_readiness
from ui_bot.runtime import DemoRuntime
from ui_bot.runtime_child import (CommandGate, JsonDashboard, SNAPSHOT_PREFIX,
                                  bot_invocation, build_snapshot)
from ui_bot.tests.test_player_position_pipeline import _Bot, _Eyes


_APP = QApplication.instance() or QApplication([])


class QuietDemoRuntime(DemoRuntime):
    """Keep the injected readiness snapshot stable across a START click."""

    def resume(self):
        if not self.started:
            raise RuntimeError("demo worker is not active")
        self.running = True


def _snapshot(*, memory_ready, pixel_ready, sequence=100,
              active_mode="waiting", running=False,
              memory_error="player position unavailable",
              pixel_error="minimap capture region unavailable"):
    player = {"x": 0.0, "z": 12.0} if memory_ready else None
    return BotSnapshot.from_mapping({
        "sequence": sequence,
        "timestamp": 1000.0 + sequence,
        "state": "RUNNING" if running else "PAUSED",
        "connection_state": "CONNECTED",
        "automation_state": "RUNNING" if running else "IDLE",
        "connected": True,
        "memory_session_valid": True,
        "memory_active": memory_ready,
        "memory_ready": memory_ready,
        "pixel_ready": pixel_ready,
        "pixel_error": "" if pixel_ready else pixel_error,
        "active_mode": active_mode,
        "source": "pixels" if active_mode == "pixel" else "memory",
        "player": player,
        "player_valid": memory_ready,
        "player_fresh": memory_ready,
        "player_error": "" if memory_ready else memory_error,
        "process_id": 77,
        "session_id": "77:1",
    })


class SharedReadinessTests(unittest.TestCase):
    def test_memory_unavailable_pixel_ready_selects_pixel(self):
        result = evaluate_start_readiness(
            game_connected=True, memory_ready=False, pixel_ready=True,
            preferred_mode="memory", memory_reason="player unavailable")
        self.assertTrue(result.can_start)
        self.assertEqual(result.selected_mode, "pixel")

    def test_both_ready_select_configured_preference(self):
        self.assertEqual(evaluate_start_readiness(
            True, True, True, "memory").selected_mode, "memory")
        self.assertEqual(evaluate_start_readiness(
            True, True, True, "minimap").selected_mode, "pixel")

    def test_both_unavailable_returns_exact_combined_reason(self):
        result = evaluate_start_readiness(
            True, False, False, "memory",
            memory_reason="player position unavailable",
            pixel_reason="capture region invalid")
        self.assertFalse(result.can_start)
        self.assertEqual(
            result.reason,
            "Memory unavailable: player position unavailable; "
            "Pixel unavailable: capture region invalid")

    def test_end_gate_uses_same_readiness_and_always_allows_stop_edge(self):
        events = []
        gate = CommandGate(events.append)
        gate.observe(False)
        gate.set_start_readiness(False, "waiting", "no targeting mode available")
        self.assertFalse(gate.allow_hotkey_toggle())
        self.assertIn("no targeting mode available", events[-1])

        gate.set_start_readiness(True, "pixel", "Pixel Mode ready")
        self.assertTrue(gate.allow_hotkey_toggle())
        gate.observe(True)
        gate.set_start_readiness(False, "waiting", "readiness lost")
        self.assertTrue(gate.allow_hotkey_toggle())


class StartButtonReadinessTests(unittest.TestCase):
    def make_window(self, preferred="memory"):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "areas.json").write_text(
            '{"cell":3,"areas":{}}', encoding="utf-8")
        runtime = QuietDemoRuntime()
        window = MainWindow(root, runtime=runtime, demo_mode=True)
        window._test_directory = directory
        window.pages["Settings"].mode.setCurrentText(preferred)
        window.show()
        return window, runtime

    def test_memory_unavailable_pixel_ready_enables_start_and_starts_pixel(self):
        window, runtime = self.make_window("memory")
        try:
            window._snapshot_received(_snapshot(
                memory_ready=False, pixel_ready=True))
            self.assertTrue(window.start_button.isEnabled())

            QTest.mouseClick(window.start_button, Qt.LeftButton)

            self.assertTrue(runtime.running)
            self.assertEqual(window.selected_mode, "pixel")
            self.assertEqual(window.mode_badge.text(), "MODE: PIXEL")
        finally:
            window.emergency_stop("test cleanup")
            window.close()

    def test_recovering_disables_start_but_keeps_manual_overrides_available(self):
        window, runtime = self.make_window("memory")
        try:
            runtime.started = True
            window.controller._desired_running = True
            window.controller._recovering = True
            window.controller._recovery_reason = "temporary player read miss"
            window.controller.state = BotState.RECOVERING
            window.controller.automation_state = AutomationState.RECOVERING
            window._refresh_controls()

            self.assertFalse(window.start_button.isEnabled())
            self.assertTrue(window.pause_button.isEnabled())
            self.assertTrue(window.stop_button.isEnabled())
            self.assertEqual(window.mode_badge.text(), "MODE: WAITING")
            self.assertIn("Recovering automatically", window.mode_reason.text())
        finally:
            window.emergency_stop("test cleanup")
            window.close()

    def test_both_ready_starts_configured_preferred_mode(self):
        for preferred, expected in (("memory", "memory"),
                                    ("minimap", "pixel")):
            with self.subTest(preferred=preferred):
                window, runtime = self.make_window(preferred)
                try:
                    window._snapshot_received(_snapshot(
                        memory_ready=True, pixel_ready=True))
                    QTest.mouseClick(window.start_button, Qt.LeftButton)
                    self.assertTrue(runtime.running)
                    self.assertEqual(window.selected_mode, expected)
                    self.assertEqual(window.mode_badge.text(),
                                     f"MODE: {expected.upper()}")
                finally:
                    window.emergency_stop("test cleanup")
                    window.close()

    def test_both_unavailable_disables_start_and_shows_reason(self):
        window, _runtime = self.make_window("memory")
        try:
            window._snapshot_received(_snapshot(
                memory_ready=False, pixel_ready=False,
                memory_error="player missing", pixel_error="capture invalid"))
            self.assertFalse(window.start_button.isEnabled())
            self.assertEqual(window.mode_badge.text(), "MODE: WAITING")
            self.assertEqual(
                window.mode_reason.text(),
                "Memory unavailable: player missing; Pixel unavailable: capture invalid")
            self.assertEqual(window.start_button.toolTip(), window.mode_reason.text())
            window.resize(window.minimumWidth(), window.minimumHeight())
            _APP.processEvents()
            self.assertLess(
                window.mode_reason.fontMetrics().horizontalAdvance(
                    window.mode_reason.text()),
                window.mode_reason.contentsRect().width())
        finally:
            window.emergency_stop("test cleanup")
            window.close()

    def test_end_hotkey_selects_same_pixel_fallback_as_start_button(self):
        window, runtime = self.make_window("memory")
        try:
            window._snapshot_received(_snapshot(
                memory_ready=False, pixel_ready=True))
            window.end_hotkey()
            self.assertTrue(runtime.running)
            self.assertEqual(window.selected_mode, "pixel")
        finally:
            window.emergency_stop("test cleanup")
            window.close()


    def test_switching_mode_disables_start_pause_but_keeps_stop(self):
        window, _runtime = self.make_window("memory")
        try:
            window._snapshot_received(_snapshot(
                memory_ready=False, pixel_ready=True))
            window.selected_mode = "pixel"
            window.controller.state = BotState.SWITCHING_MODE
            window.controller.automation_state = AutomationState.IDLE
            window._refresh_controls()
            self.assertFalse(window.start_button.isEnabled())
            self.assertFalse(window.pause_button.isEnabled())
            self.assertTrue(window.stop_button.isEnabled())
            self.assertEqual(window.mode_badge.text(), "MODE: WAITING")
            self.assertEqual(window.mode_reason.text(),
                             "Switching to Pixel Mode…")
        finally:
            window.emergency_stop("test cleanup")
            window.close()

    def test_pixel_mode_omits_selected_polygon_and_shows_notice(self):
        window, runtime = self.make_window("memory")
        try:
            window.pages["Settings"].area.setCurrentText("depth2")
            window._snapshot_received(_snapshot(
                memory_ready=False, pixel_ready=True))
            self.assertEqual(window._runtime_options("pixel")["area"], "")
            QTest.mouseClick(window.start_button, Qt.LeftButton)
            self.assertTrue(runtime.running)
            self.assertEqual(window.mode_reason.text(),
                             "Polygon unavailable in Pixel Mode")
            self.assertIn("Polygon unavailable in Pixel Mode",
                          window.activity_log.toPlainText())
        finally:
            window.emergency_stop("test cleanup")
            window.close()

    def test_running_pixel_fallback_auto_handoffs_to_preferred_memory_area(self):
        window, _runtime = self.make_window("memory")
        try:
            window.pages["Settings"].area.addItem("depth2")
            window.pages["Settings"].area.setCurrentText("depth2")
            window.selected_mode = "pixel"
            window.controller.automation_state = AutomationState.RUNNING
            # Readiness may rise while the Memory -> Pixel fallback replacement
            # is still switching; the first running Pixel snapshot must hand off.
            window._last_memory_ready = True
            with (patch.object(window.controller, "pause", return_value=True) as pause,
                  patch.object(window.controller, "start", return_value=True) as start):
                window.apply_snapshot(_snapshot(
                    memory_ready=True, pixel_ready=True, active_mode="pixel",
                    running=True, sequence=101))

            pause.assert_called_once_with()
            start.assert_called_once()
            options = start.call_args.args[0]
            self.assertEqual(options["mode"], "memory")
            self.assertEqual(options["area"], "depth2")
            self.assertEqual(window.selected_mode, "memory")
            self.assertIn("Memory scan ready; switching Pixel fallback",
                          window.activity_log.toPlainText())
        finally:
            window.emergency_stop("test cleanup")
            window.close()

    def test_explicit_pixel_preference_does_not_auto_handoff(self):
        window, _runtime = self.make_window("minimap")
        try:
            window.selected_mode = "pixel"
            window.controller.automation_state = AutomationState.RUNNING
            window._last_memory_ready = False
            with (patch.object(window.controller, "pause", return_value=True) as pause,
                  patch.object(window.controller, "start", return_value=True) as start):
                window.apply_snapshot(_snapshot(
                    memory_ready=True, pixel_ready=True, active_mode="pixel",
                    running=True, sequence=101))

            pause.assert_not_called()
            start.assert_not_called()
            self.assertEqual(window.selected_mode, "pixel")
            self.assertIn("Memory Mode is available; Stop and start again to switch.",
                          window.activity_log.toPlainText())
        finally:
            window.emergency_stop("test cleanup")
            window.close()


class ChildModeSafetyTests(unittest.TestCase):
    @staticmethod
    def _world(player):
        return {
            "version": 2, "captured_at": __import__("time").time(),
            "player": player,
            "player_valid": player is not None,
            "player_error": "" if player is not None else "owner unavailable",
            "entities": (), "target": None,
            "zone": {"name": "", "kind": "none", "points": [],
                     "circles": [], "safety_margin": 0.0,
                     "auto_return": True, "cell_size": 3.0},
            "route": (), "total": 0, "hostile": 0, "unique": 0,
            "inside_zone": 0, "valid_targets": 0,
            "connection_state": "CONNECTED", "error": "",
        }

    def test_pixel_mode_runs_without_player_position(self):
        raw = build_snapshot(
            {"running": True, "state": "chasing", "source": "pixels",
             "stick": (0.8, -0.2), "attack": True},
            None, 1, [], scan_world=self._world(None), scan_is_new=True,
            process_id=77, session_id="77:1", preferred_mode="minimap",
            pixel_ready=True)
        snapshot = BotSnapshot.from_mapping(raw)
        self.assertEqual(snapshot.automation_state, AutomationState.RUNNING)
        self.assertEqual(snapshot.active_mode, "pixel")
        self.assertEqual(raw["control"], {"stick": [0.8, -0.2], "attack": True})
        self.assertFalse(snapshot.memory_ready)
        self.assertTrue(snapshot.pixel_ready)

    def test_pixel_invocation_uses_minimap_and_excludes_memory_area(self):
        argv, active_area, notice = bot_invocation(
            "minimap", "depth2", "minimap_bot.py")
        self.assertEqual(argv, ["minimap_bot.py", "--minimap"])
        self.assertEqual(active_area, "")
        self.assertEqual(notice, "Polygon unavailable in Pixel Mode")

    def test_memory_failure_in_memory_mode_releases_inputs(self):
        raw = build_snapshot(
            {"running": True, "state": "chasing", "source": "memory",
             "stick": (0.8, -0.2), "attack": True},
            None, 1, [], scan_world=self._world(None), scan_is_new=True,
            process_id=77, session_id="77:1", preferred_mode="memory",
            pixel_ready=True)
        snapshot = BotSnapshot.from_mapping(raw)
        self.assertEqual(snapshot.automation_state, AutomationState.PAUSED)
        self.assertEqual(raw["control"], {"stick": [0.0, 0.0], "attack": False})

    def test_explicit_pixel_dashboard_keeps_background_memory_monitor(self):
        output = io.StringIO()
        eyes = _Eyes(player=(0.0, 4.0, 12.0))
        dashboard = JsonDashboard(
            _Bot, bot_mode="minimap", expected_pid=77, session_id="77:1",
            monitor_eyes=eyes, preferred_mode="minimap", pixel_ready=True)
        with redirect_stdout(output):
            dashboard.update(None, False, "paused", force=True)
        payload = json.loads(output.getvalue().split(SNAPSHOT_PREFIX, 1)[1])
        self.assertTrue(payload["memory_ready"])
        self.assertTrue(payload["pixel_ready"])
        self.assertEqual(payload["player"], {"x": 0.0, "z": 12.0})


if __name__ == "__main__":
    unittest.main()
