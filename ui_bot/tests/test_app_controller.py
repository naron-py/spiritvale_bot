import unittest

from ui_bot.app_controller import AppController
from ui_bot.model import (AutomationState, BotSnapshot, BotState, ConnectionState,
                          FAILURE_POLICIES, FailureCode)
from ui_bot.runtime import WorkerExit, WorkerLifetime, WorkerPurpose


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.running = False

    def start(self, options):
        self.calls.append(("start", dict(options)))
        self.running = True

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
        self.running = False

    def emergency_stop(self):
        self.calls.append(("emergency_stop",))
        self.running = False

    def reset_emergency(self):
        self.calls.append(("reset_emergency",))

    def update_controller_config(self, config):
        self.calls.append(("configure", dict(config)))

    def restart_current(self, reason):
        self.calls.append(("restart_current", reason))


class AppControllerTests(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.events = []
        self.controller = AppController(self.runtime, self.events.append)

    def test_repeated_start_is_idempotent(self):
        self.assertTrue(self.controller.start({"mode": "memory"}))
        self.assertFalse(self.controller.start({"mode": "memory"}))

        self.assertEqual(self.runtime.calls,
                         [("start", {"mode": "memory"})])
        self.assertEqual(self.controller.state, BotState.STARTING)

    def test_runtime_controller_update_does_not_restart_worker(self):
        self.controller.start({"mode": "memory"})
        config = {"buff_slots": [{"id": "one"}], "attack_slots": []}

        self.assertTrue(self.controller.update_controller_config(config))

        self.assertEqual([call[0] for call in self.runtime.calls],
                         ["start", "configure"])

    def test_pause_and_stop_are_idempotent_and_release_once(self):
        self.controller.start({})
        self.controller.accept_snapshot(
            BotSnapshot.safe(BotState.RUNNING, "running"))

        self.assertTrue(self.controller.pause())
        self.assertFalse(self.controller.pause())
        self.assertTrue(self.controller.stop())
        self.assertFalse(self.controller.stop())

        self.assertEqual([call[0] for call in self.runtime.calls],
                         ["start", "pause", "stop"])
        self.assertEqual(self.controller.state, BotState.STOPPED)

    def test_paused_worker_resumes_without_starting_a_second_worker(self):
        self.controller.start({})
        self.controller.accept_snapshot(
            BotSnapshot.safe(BotState.RUNNING, "running"))
        self.controller.pause()

        self.assertTrue(self.controller.start({"ignored": True}))

        self.assertEqual([call[0] for call in self.runtime.calls],
                         ["start", "pause", "resume"])

    def test_mode_switch_disables_start_and_pause_until_replacement_snapshot(self):
        self.controller._worker_started = True
        self.controller.automation_state = AutomationState.PAUSED
        self.runtime.select_mode = lambda _options: True

        self.assertTrue(self.controller.start({"mode": "minimap"}))
        self.assertEqual(self.controller.state, BotState.SWITCHING_MODE)
        self.assertFalse(self.controller.start({"mode": "memory"}))
        self.assertFalse(self.controller.pause())

        disposition = self.controller.worker_finished(WorkerExit(
            WorkerPurpose.MONITOR, WorkerLifetime.PERSISTENT, True, 0,
            "NormalExit", generation=1, mode_switch=True))
        self.assertEqual(disposition.value, "normal_shutdown")
        self.assertEqual(self.controller.state, BotState.SWITCHING_MODE)

        self.controller.accept_snapshot(BotSnapshot.from_mapping({
            "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "pixel_ready": True,
            "active_mode": "pixel", "source": "pixels",
        }))
        self.assertEqual(self.controller.state, BotState.RUNNING)

    def test_unexpected_worker_crash_recovers_when_run_was_requested(self):
        self.controller.start({"mode": "memory", "area": "depth2"})
        disposition = self.controller.worker_finished(WorkerExit(
            WorkerPurpose.MONITOR, WorkerLifetime.PERSISTENT, False, 62097,
            "CrashExit", generation=3))

        self.assertEqual(disposition.value, "failure")
        self.assertFalse(self.controller.emergency_latched)
        self.assertTrue(self.controller.monitoring)
        self.assertEqual(self.controller.connection_state.value, "ERROR")
        self.assertEqual(self.controller.automation_state,
                         AutomationState.RECOVERING)
        self.assertEqual(self.controller.state, BotState.RECOVERING)
        self.assertTrue(self.controller.desired_running)
        self.assertNotIn(("emergency_stop",), self.runtime.calls)
        self.assertIn("62097", self.controller.last_error)

    def test_explicit_pixel_worker_restart_resumes_without_player_memory(self):
        self.controller.start({"mode": "minimap"})
        self.controller.worker_finished(WorkerExit(
            WorkerPurpose.MONITOR, WorkerLifetime.PERSISTENT,
            stop_requested=False, process_gone=False,
            exit_code=1, exit_status="CrashExit",
            last_valid_snapshot_time=None))

        base = {
            "state": "PAUSED", "automation_state": "PAUSED",
            "connection_state": "CONNECTED", "connected": True,
            "process_id": 99, "session_id": "99:new",
            "source": "minimap", "active_mode": "waiting",
            "pixel_ready": True, "memory_ready": False,
            "player_valid": False, "player_fresh": False,
        }
        for sequence in range(1, 4):
            self.controller.accept_snapshot(dict(
                base, sequence=sequence, timestamp=float(sequence)))

        self.assertEqual(self.runtime.calls.count(("resume",)), 1)
        self.assertEqual(self.controller.automation_state,
                         AutomationState.RECOVERING)

        self.controller.accept_snapshot(dict(
            base, sequence=4, timestamp=4.0, state="RUNNING",
            automation_state="RUNNING", active_mode="minimap"))
        self.assertEqual(self.controller.automation_state,
                         AutomationState.RUNNING)

    def test_monitor_disconnect_preserves_run_intent_and_valid_snapshot_resumes(self):
        self.controller = AppController(self.runtime, self.events.append,
                                        recovery_valid_snapshots=1)
        self.controller.start({"mode": "memory", "area": "depth2"})
        self.controller.monitor_status(ConnectionState.DISCONNECTED,
                                       "game process unavailable")

        self.assertEqual(self.controller.state, BotState.RECOVERING)
        self.assertTrue(self.controller.desired_running)
        self.controller.accept_snapshot({
            "sequence": 10, "state": "PAUSED", "automation_state": "IDLE",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "process_id": 8, "session_id": "8:new", "active_mode": "waiting",
            "source": "memory",
        })
        self.assertEqual(self.runtime.calls.count(("resume",)), 1)

    def test_user_pause_clears_run_intent_and_does_not_auto_resume(self):
        self.controller.start({"mode": "memory"})
        self.controller.accept_snapshot({
            "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "active_mode": "memory", "source": "memory",
        })
        self.assertTrue(self.controller.pause())
        before = self.runtime.calls.count(("resume",))
        self.controller.monitor_status(ConnectionState.DISCONNECTED, "temporary")
        self.controller.accept_snapshot({
            "state": "PAUSED", "automation_state": "IDLE",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "active_mode": "waiting", "source": "memory",
        })
        self.assertFalse(self.controller.desired_running)
        self.assertEqual(self.runtime.calls.count(("resume",)), before)

    def test_pause_during_reconnect_keeps_intent_when_transport_fails(self):
        controller = AppController(self.runtime)
        controller._worker_started = True
        controller._desired_running = True
        controller._recovering = True
        controller.state = BotState.RECOVERING
        controller.automation_state = AutomationState.RECOVERING

        def unavailable():
            raise RuntimeError("worker is reconnecting")

        self.runtime.pause = unavailable
        self.assertTrue(controller.pause())
        self.assertFalse(controller.desired_running)
        self.assertEqual(controller.state, BotState.PAUSED)
        self.assertEqual(controller.automation_state, AutomationState.PAUSED)
        self.assertTrue(any(call[0] == "restart_current"
                            for call in self.runtime.calls))

    def test_progress_watchdog_classifies_stalled_target_and_enters_recovery(self):
        now = [100.0]
        controller = AppController(
            self.runtime, self.events.append, progress_timeout_s=10.0,
            clock=lambda: now[0])
        controller.start({"mode": "memory", "area": "depth2"})
        raw = {
            "sequence": 1, "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "active_mode": "memory", "source": "memory",
            "target": {"id": "84", "kind": "monster", "x": 6.0,
                       "z": 2.0, "valid": True, "stable_id": True,
                       "valid_pointer": True, "alive": True,
                       "distance": 5.0},
        }
        controller.accept_snapshot(raw)
        now[0] += 11.0
        raw["sequence"] = 2
        controller.accept_snapshot(raw)

        self.assertEqual(controller.state, BotState.RECOVERING)
        self.assertEqual(self.runtime.calls.count(("pause",)), 1)
        self.assertIn("target/navigation", controller.recovery_reason)
        now[0] += 21.0
        controller.tick_recovery()
        self.assertFalse(any(call[0] == "restart_current"
                             for call in self.runtime.calls))

    def test_progress_watchdog_resets_after_player_moves(self):
        now = [200.0]
        controller = AppController(
            self.runtime, progress_timeout_s=10.0, clock=lambda: now[0])
        controller.start({"mode": "memory"})
        raw = {
            "sequence": 1, "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "active_mode": "memory", "source": "memory",
        }
        controller.accept_snapshot(raw)
        now[0] += 9.0
        raw["sequence"] = 2
        raw["player"] = {"x": 2.0, "z": 2.0}
        controller.accept_snapshot(raw)
        now[0] += 9.0
        raw["sequence"] = 3
        controller.accept_snapshot(raw)

        self.assertNotEqual(controller.state, BotState.RECOVERING)
        self.assertEqual(self.runtime.calls.count(("pause",)), 0)

    def test_progress_watchdog_ignores_no_destination_with_empty_entities(self):
        now = [250.0]
        controller = AppController(
            self.runtime, progress_timeout_s=10.0, clock=lambda: now[0])
        controller.start({"mode": "memory"})
        raw = {
            "sequence": 1, "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "active_mode": "memory", "source": "memory",
            "entities": [], "path": [], "target": None,
            "control": {"stick": [0.0, 0.0]},
        }
        controller.accept_snapshot(raw)
        now[0] += 60.0
        raw["sequence"] = 2
        controller.accept_snapshot(raw)

        self.assertEqual(controller.state, BotState.RUNNING)
        self.assertEqual(self.runtime.calls.count(("pause",)), 0)

    def test_player_read_recovery_waits_then_abandons_without_worker_restart(self):
        now = [300.0]
        controller = AppController(
            self.runtime, self.events.append, recovery_valid_snapshots=2,
            recovery_restart_s=10.0, recovery_max_s=30.0,
            clock=lambda: now[0])
        controller.start({"mode": "memory", "area": "depth2"})
        valid = {
            "sequence": 1, "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "process_id": 42, "session_id": "s1",
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 10.0, "z": 20.0},
            "player_valid": True, "player_fresh": True,
            "player_read_version": 1,
            "active_mode": "memory", "source": "memory",
            "zone": {"name": "depth2", "valid": True},
        }
        controller.accept_snapshot(valid)
        for sequence in (2, 3, 4):
            invalid = dict(valid)
            invalid.update({
                "sequence": sequence, "player_read_version": sequence,
                "memory_ready": False, "player": None,
                "player_valid": False, "player_fresh": False,
                "player_error": "temporary read miss"})
            controller.accept_snapshot(invalid)

        now[0] += 11.0
        controller.tick_recovery()
        self.assertFalse(any(call[0] == "restart_current"
                             for call in self.runtime.calls))
        self.assertFalse(controller.emergency_latched)

        now[0] += 20.0
        controller.tick_recovery()
        self.assertEqual(controller.state, BotState.SAFE_STOP)
        self.assertTrue(controller.emergency_latched)
        log = "\n".join(self.events)
        self.assertIn("last_valid_seq=1", log)
        self.assertIn("last_valid_player=(10.0, 20.0)", log)
        self.assertIn("zone='depth2'", log)
        self.assertIn("recovery window exhausted", log)

    def test_malformed_control_stick_enters_recovery_instead_of_escaping(self):
        now = [500.0]
        controller = AppController(
            self.runtime, progress_timeout_s=10.0, clock=lambda: now[0])
        controller.start({"mode": "memory"})
        raw = {
            "sequence": 1, "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_active": True,
            "memory_ready": True, "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "active_mode": "memory", "source": "memory",
            "control": {"stick": [0.0, 0.0]},
        }
        self.assertTrue(controller.accept_snapshot(raw))
        now[0] += 11.0
        raw["sequence"] = 2
        raw["control"] = {"stick": ["broken", object()]}

        self.assertFalse(controller.accept_snapshot(raw))
        self.assertEqual(controller.state, BotState.RECOVERING)
        self.assertFalse(controller.emergency_latched)
        self.assertIn(("pause",), self.runtime.calls)

    def test_stop_during_switch_cancels_run_intent_and_late_running_frame(self):
        controller = AppController(self.runtime)
        controller._worker_started = True
        controller._desired_running = True
        controller.state = BotState.SWITCHING_MODE
        controller.automation_state = AutomationState.IDLE

        self.assertTrue(controller.stop())
        self.assertFalse(controller.desired_running)
        self.assertIn(("stop",), self.runtime.calls)
        self.assertTrue(controller.accept_snapshot({
            "sequence": 1, "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_ready": True,
            "player": {"x": 1.0, "z": 2.0},
            "player_valid": True, "player_fresh": True,
            "active_mode": "memory", "source": "memory",
        }))
        self.assertEqual(controller.state, BotState.STOPPED)
        self.assertIn(("pause",), self.runtime.calls)

    def test_pixel_mode_player_loss_does_not_pause_automation(self):
        self.controller._worker_started = True
        self.controller._desired_running = True
        self.controller._last_options = {"mode": "minimap"}
        self.controller.automation_state = AutomationState.RUNNING
        self.controller.state = BotState.RUNNING

        self.assertTrue(self.controller.accept_snapshot({
            "state": "RUNNING", "automation_state": "RUNNING",
            "connection_state": "CONNECTED", "connected": True,
            "memory_session_valid": True, "memory_ready": False,
            "pixel_ready": True, "active_mode": "pixel", "source": "pixels",
            "player": None, "player_valid": False, "player_fresh": False,
            "player_error": "owner unavailable",
        }))

        self.assertEqual(self.controller.automation_state,
                         AutomationState.RUNNING)
        self.assertEqual(self.controller.state, BotState.RUNNING)
        self.assertNotIn(("pause",), self.runtime.calls)
        self.assertTrue(any("action=continue" in event for event in self.events))
        self.assertFalse(any("action=fallback" in event for event in self.events))

    def test_emergency_stop_latches_until_explicit_reset(self):
        self.controller.start({})

        self.assertTrue(self.controller.emergency_stop("user hotkey"))
        self.assertFalse(self.controller.start({}))
        self.assertEqual(self.controller.state, BotState.EMERGENCY_STOP)

        self.controller.reset_emergency()
        self.assertEqual(self.controller.state, BotState.STOPPED)
        self.assertTrue(self.controller.start({}))
        self.assertEqual([call[0] for call in self.runtime.calls],
                         ["start", "emergency_stop", "reset_emergency", "start"])

    def test_controller_failure_safe_stops_and_latches(self):
        self.controller.start({})

        self.controller.fail(FailureCode.CONTROLLER_COMMAND,
                             RuntimeError("send failed"))

        self.assertEqual(self.controller.state, BotState.SAFE_STOP)
        self.assertTrue(self.controller.emergency_latched)
        self.assertFalse(self.controller.desired_running)
        self.assertIn("controller", self.controller.last_error.lower())
        self.assertTrue(any("send failed" in event for event in self.events))

    def test_resume_transport_failure_safe_stops(self):
        self.controller.start({})
        self.controller.accept_snapshot(BotSnapshot.safe(BotState.RUNNING))
        self.controller.pause()

        def fail_resume():
            raise RuntimeError("worker vanished")

        self.runtime.resume = fail_resume
        self.assertFalse(self.controller.start({}))
        self.assertEqual(self.controller.automation_state,
                         AutomationState.SAFE_STOP)
        self.assertTrue(self.controller.emergency_latched)

    def test_pause_uses_running_automation_state_even_if_status_paused(self):
        self.controller.start({})
        self.controller.accept_snapshot(BotSnapshot.safe(BotState.PAUSED))
        self.assertEqual(self.controller.automation_state,
                         AutomationState.RUNNING)
        self.assertTrue(self.controller.pause())
        self.assertEqual(self.runtime.calls[-1], ("pause",))

    def test_stop_before_start_is_a_safe_no_op(self):
        self.assertFalse(self.controller.stop())
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.controller.state, BotState.STOPPED)

    def test_every_failure_category_uses_recovery_or_latched_policy(self):
        transient = {
            FailureCode.WORKER_STOPPED,
            FailureCode.MALFORMED_SNAPSHOT,
        }
        for code in FailureCode:
            with self.subTest(code=code):
                runtime = FakeRuntime()
                controller = AppController(runtime)
                controller.start({})

                controller.fail(code, RuntimeError(code.value))

                if code in transient:
                    self.assertEqual(runtime.calls[-1], ("pause",))
                    self.assertEqual(controller.state, BotState.RECOVERING)
                    self.assertFalse(controller.emergency_latched)
                else:
                    self.assertEqual(runtime.calls[-1], ("emergency_stop",))
                    self.assertEqual(controller.state,
                                     FAILURE_POLICIES[code].safe_state)
                    self.assertTrue(controller.emergency_latched)


if __name__ == "__main__":
    unittest.main()
