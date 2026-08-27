"""UI-side lifecycle state machine.

The controller is intentionally independent of Qt. Widgets call this object, while
the runtime port owns the child process. That keeps repeated button clicks and
failure transitions deterministic and unit-testable.
"""

from __future__ import annotations

import math
import time
import traceback
from typing import Callable, Mapping, Protocol

from .model import (AutomationState, BotSnapshot, BotState, ConnectionState,
                    FAILURE_POLICIES, FailureCode, SnapshotError)
from .readiness import normalize_mode
from .runtime import WorkerDisposition, classify_worker_exit


class RuntimePort(Protocol):
    def attach(self, options: Mapping[str, object]) -> None: ...
    def start(self, options: Mapping[str, object]) -> None: ...
    def resume(self) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def emergency_stop(self) -> None: ...
    def reset_emergency(self) -> None: ...
    def restart_current(self, reason: str) -> None: ...


class AppController:
    def __init__(self, runtime: RuntimePort,
                 log: Callable[[str], None] | None = None,
                 recovery_valid_snapshots: int = 3,
                 progress_timeout_s: float = 45.0,
                 recovery_restart_s: float = 20.0,
                 recovery_max_s: float = 300.0,
                 clock: Callable[[], float] | None = None):
        self.runtime = runtime
        self.log = log or (lambda _message: None)
        self.state = BotState.STOPPED
        self.connection_state = ConnectionState.DISCONNECTED
        self.automation_state = AutomationState.IDLE
        self.last_snapshot = BotSnapshot.safe()
        self.last_error = ""
        self._worker_started = False
        self._emergency_latched = False
        self._process_id = 0
        self._session_id = ""
        self._desired_running = False
        self._last_options: dict[str, object] = {}
        self._recovering = False
        self._recovery_reason = ""
        self._recovery_started_at = 0.0
        self._recovery_attempts = 0
        self._recovery_valid_streak = 0
        self._recovery_resume_requested = False
        self._recovery_start_sequence = 0
        self._recovery_resume_sequence = 0
        self._last_accepted_sequence = 0
        self._recovery_restart_s = max(1.0, float(recovery_restart_s))
        self._recovery_max_s = max(
            self._recovery_restart_s + 1.0, float(recovery_max_s))
        self._recovery_next_restart_at = 0.0
        self._recovery_actions: list[str] = []
        self._recovery_valid_snapshots = max(1, int(recovery_valid_snapshots))
        self._last_valid_sequence = 0
        self._last_valid_player = None
        self._clock = clock or time.monotonic
        self._progress_timeout_s = max(5.0, float(progress_timeout_s))
        self._progress_at = self._clock()
        self._progress_player = None
        self._progress_target_id = None
        self._progress_target_distance = None

    @property
    def emergency_latched(self) -> bool:
        return self._emergency_latched

    @property
    def monitoring(self) -> bool:
        return self._worker_started

    @property
    def desired_running(self) -> bool:
        return self._desired_running

    @property
    def recovery_reason(self) -> str:
        return self._recovery_reason

    def tick_recovery(self) -> None:
        """Advance bounded recovery even when no worker snapshots arrive."""
        self._check_recovery_deadline(self.last_snapshot)

    def attach(self, options: Mapping[str, object]) -> bool:
        if self._worker_started or self._emergency_latched:
            return False
        try:
            attach = getattr(self.runtime, "attach", None)
            if attach is None:
                self.runtime.start(dict(options))
            else:
                attach(dict(options))
        except Exception as exc:
            self.fail(FailureCode.WORKER_EXCEPTION, exc)
            return False
        self._worker_started = True
        self.connection_state = ConnectionState.CONNECTING
        self.automation_state = AutomationState.IDLE
        self.state = BotState.STOPPED
        self.last_error = ""
        self.log("[Monitor] Read-only game monitor attaching; automation remains idle.")
        return True

    def start(self, options: Mapping[str, object]) -> bool:
        if self._emergency_latched:
            self.log("[Safety] Start blocked: Emergency Stop must be reset.")
            return False
        if self.state == BotState.SWITCHING_MODE:
            self.log("[Bot] Start ignored: mode switch is still in progress.")
            return False
        self._last_options = dict(options)
        self._desired_running = True
        if self._worker_started and self.automation_state in (
                AutomationState.IDLE, AutomationState.PAUSED,
                AutomationState.RECOVERING):
            try:
                select_mode = getattr(self.runtime, "select_mode", None)
                restarting = bool(select_mode(dict(options))) if select_mode else False
                if not restarting:
                    self.runtime.resume()
            except Exception as exc:
                self.fail(FailureCode.CONTROLLER_COMMAND, exc)
                return False
            self.automation_state = (
                AutomationState.IDLE if restarting else AutomationState.RUNNING)
            self.state = (BotState.SWITCHING_MODE if restarting
                          else BotState.STARTING)
            self._clear_recovery()
            self.log("[Bot] Mode switch waiting for old worker exit."
                     if restarting else "[Bot] Resume requested.")
            return True
        if self._worker_started or self.state in (BotState.STARTING, BotState.RUNNING):
            self.log("[Bot] Start ignored: worker already active.")
            return False
        try:
            self.runtime.start(dict(options))
        except Exception as exc:
            self.fail(FailureCode.WORKER_EXCEPTION, exc)
            return False
        self._worker_started = True
        self.automation_state = AutomationState.RUNNING
        self.state = BotState.STARTING
        self.last_error = ""
        self._clear_recovery()
        self.log("[Bot] Start requested.")
        return True

    def pause(self) -> bool:
        if (not self._worker_started or
                self.automation_state not in (
                    AutomationState.RUNNING, AutomationState.RECOVERING)):
            return False
        self._desired_running = False
        self._clear_recovery()
        self.state = BotState.PAUSED
        self.automation_state = AutomationState.PAUSED
        try:
            self.runtime.pause()
        except Exception as exc:
            try:
                self.runtime.restart_current(
                    f"pause command unavailable; retire worker safely: {exc}")
            except Exception as restart_error:
                self.log(f"[Safety] Pause transport/restart failed: {restart_error}")
            self.log(f"[Safety] Pause intent retained while worker reconnects: {exc}")
        self.log("[Safety] Pause requested; inputs released by worker.")
        return True

    def stop(self) -> bool:
        had_worker = self._worker_started
        was_idle = self.automation_state == AutomationState.IDLE
        was_switching = self.state == BotState.SWITCHING_MODE
        self._desired_running = False
        self._clear_recovery()
        self.automation_state = AutomationState.IDLE
        self.state = BotState.STOPPED
        if not had_worker or (was_idle and not was_switching):
            return False
        try:
            self.runtime.stop()
        except Exception as exc:
            try:
                self.runtime.restart_current(
                    f"stop command unavailable; retire worker safely: {exc}")
            except Exception as restart_error:
                self.log(f"[Safety] Stop transport/restart failed: {restart_error}")
            self.log(f"[Safety] Stop intent retained while worker reconnects: {exc}")
        self.log("[Safety] Automation stopped and inputs released; read-only monitoring continues.")
        return True

    def emergency_stop(self, reason: str = "Emergency Stop") -> bool:
        if self._emergency_latched:
            return False
        self._emergency_latched = True
        self._desired_running = False
        self._clear_recovery()
        try:
            self.runtime.emergency_stop()
        except Exception:
            self.log("[Safety] Runtime emergency cleanup raised:\n" + traceback.format_exc())
        self._worker_started = False
        self.automation_state = AutomationState.SAFE_STOP
        self.state = BotState.EMERGENCY_STOP
        self.last_error = reason
        self.last_snapshot = BotSnapshot.safe(BotState.EMERGENCY_STOP, reason)
        self.log(f"[EMERGENCY] {reason}; commands blocked until explicit reset.")
        return True

    def reset_emergency(self) -> bool:
        if not self._emergency_latched:
            return False
        self.runtime.reset_emergency()
        self._emergency_latched = False
        self.connection_state = ConnectionState.DISCONNECTED
        self.automation_state = AutomationState.IDLE
        self.state = BotState.STOPPED
        self.last_error = ""
        self.last_snapshot = BotSnapshot.safe(BotState.STOPPED, "Emergency reset")
        self.log("[Safety] Emergency latch reset; bot remains stopped.")
        return True

    def accept_snapshot(self, snapshot: BotSnapshot | Mapping[str, object]) -> bool:
        try:
            if not isinstance(snapshot, BotSnapshot):
                snapshot = BotSnapshot.from_mapping(snapshot)
        except SnapshotError as exc:
            self.fail(FailureCode.MALFORMED_SNAPSHOT, exc)
            return False
        if self._emergency_latched:
            self.log("[Safety] Snapshot discarded after Emergency Stop.")
            return False
        old_connection = self.connection_state
        if snapshot.session_id:
            if (self._session_id == snapshot.session_id
                    and self._process_id not in (0, snapshot.process_id)):
                self.log("[Monitor] Snapshot discarded: PID changed inside one session.")
                return False
            if self._session_id and self._session_id != snapshot.session_id:
                self.log("[Monitor] Snapshot discarded: monitor session changed without reconnect.")
                return False
            if not self._session_id:
                self._session_id = snapshot.session_id
                self._process_id = snapshot.process_id
                self._last_accepted_sequence = 0
        if (snapshot.sequence > 0
                and snapshot.sequence <= self._last_accepted_sequence):
            self.log(
                f"[Monitor] Snapshot discarded: non-increasing sequence "
                f"{snapshot.sequence} <= {self._last_accepted_sequence}.")
            return False
        if snapshot.sequence > 0:
            self._last_accepted_sequence = snapshot.sequence
        if snapshot.player_valid and snapshot.player_fresh and snapshot.player is not None:
            self._last_valid_sequence = snapshot.sequence
            self._last_valid_player = snapshot.player
        active_mode = normalize_mode(snapshot.active_mode)
        if active_mode == "waiting":
            active_mode = normalize_mode(
                self._last_options.get("mode", snapshot.source))
        lost_player_while_running = bool(
            self._desired_running
            and self.automation_state in (
                AutomationState.RUNNING, AutomationState.RECOVERING)
            and snapshot.memory_session_valid
            and active_mode != "pixel"
            and (not snapshot.player_valid or not snapshot.player_fresh
                 or snapshot.player is None))
        if lost_player_while_running:
            if not self._recovering:
                try:
                    self.runtime.pause()
                except Exception as exc:
                    self.fail(FailureCode.CONTROLLER_COMMAND, exc)
                    return False
                self._begin_recovery(
                    snapshot.player_error or "player position unavailable",
                    snapshot)
            else:
                self._recovery_valid_streak = 0
                self._recovery_resume_requested = False
        elif not self._recovering and not self._observe_progress(snapshot):
            return False
        self.last_snapshot = snapshot
        self.connection_state = snapshot.connection_state
        self._worker_started = True
        desired_mode = active_mode
        recovery_source_ready = bool(
            snapshot.pixel_ready if desired_mode == "pixel"
            else (snapshot.memory_session_valid and snapshot.memory_ready
                  and snapshot.player_valid and snapshot.player_fresh
                  and snapshot.player is not None))
        valid_recovery_snapshot = bool(
            self._recovering and self._desired_running
            and snapshot.sequence > self._recovery_start_sequence
            and recovery_source_ready)
        if valid_recovery_snapshot:
            self._recovery_valid_streak += 1
            if (self._recovery_valid_streak >= self._recovery_valid_snapshots
                    and not self._recovery_resume_requested):
                try:
                    self.runtime.resume()
                except Exception as exc:
                    self._recovery_attempts += 1
                    self._recovery_valid_streak = 0
                    self.log(f"[Recovery] Resume attempt failed: {exc}; "
                             "fresh snapshots continue.")
                else:
                    self._recovery_attempts += 1
                    self._recovery_resume_requested = True
                    self._recovery_resume_sequence = snapshot.sequence
                    self._recovery_actions.append(
                        f"resume requested after snapshot {snapshot.sequence}")
                    self.log(
                        f"[Recovery] {self._recovery_valid_streak} consecutive valid "
                        "snapshots; resuming previous automation automatically.")
        if (not lost_player_while_running and self._desired_running
                and snapshot.automation_state == AutomationState.RUNNING):
            # The terminal End hotkey can start automation without a button
            # command, so accepted worker state is authoritative here too.
            self.automation_state = AutomationState.RUNNING
            if (self._recovering and self._recovery_resume_requested
                    and normalize_mode(snapshot.active_mode or snapshot.source)
                    == desired_mode
                    and recovery_source_ready
                    and snapshot.sequence > self._recovery_resume_sequence):
                elapsed = max(0.0, self._clock() - self._recovery_started_at)
                self.log(f"[Recovery] RUNNING confirmed after {elapsed:.1f}s and "
                         f"{self._recovery_attempts} resume attempt(s).")
                self._clear_recovery()
        elif (snapshot.automation_state == AutomationState.RUNNING
              and not self._desired_running):
            try:
                self.runtime.pause()
            except Exception as exc:
                self.fail(FailureCode.CONTROLLER_COMMAND, exc)
                return False
            self.automation_state = AutomationState.IDLE
            self.state = BotState.STOPPED
            self.log("[Safety] Late RUNNING snapshot suppressed after user stop/pause.")
        elif (self.automation_state == AutomationState.RUNNING
              and snapshot.automation_state == AutomationState.PAUSED):
            self.automation_state = AutomationState.PAUSED
        if self._recovering:
            self.automation_state = AutomationState.RECOVERING
            self.state = BotState.RECOVERING
        elif self.automation_state == AutomationState.RUNNING:
            self.state = snapshot.state
        elif self.automation_state == AutomationState.PAUSED:
            self.state = BotState.PAUSED
        else:
            self.state = BotState.STOPPED
        if old_connection != self.connection_state:
            reason = (snapshot.error or snapshot.player_error
                      or "fresh current-session snapshot")
            self.log(f"[Connection] old={old_connection.value} "
                     f"new={self.connection_state.value} reason={reason}")
        self._check_recovery_deadline(snapshot)
        return True

    def _begin_recovery(self, reason: str,
                        snapshot: BotSnapshot | None = None) -> None:
        first_attempt = not self._recovering
        self._recovering = True
        self._recovery_reason = str(reason)
        if first_attempt:
            self._recovery_started_at = self._clock()
            self._recovery_next_restart_at = (
                self._recovery_started_at + self._recovery_restart_s)
            self._recovery_actions = [
                "inputs released and fresh snapshots continued"]
            self._recovery_attempts = 0
        else:
            self._recovery_actions.append(
                f"recovery continued: {self._recovery_reason}")
        self._recovery_valid_streak = 0
        self._recovery_resume_requested = False
        self._recovery_resume_sequence = 0
        self.automation_state = AutomationState.RECOVERING
        self.state = BotState.RECOVERING
        current = snapshot or self.last_snapshot
        self._recovery_start_sequence = current.sequence
        self.log(
            f"[Recovery] inputs released; reason={self._recovery_reason}; "
            f"state_before=RUNNING last_valid_seq={self._last_valid_sequence} "
            f"last_valid_player={self._last_valid_player} "
            f"snapshot={current.sequence} zone={current.zone.name or '-'} "
            f"entities={len(current.entities)} "
            f"candidates={len(current.monsters_in_zone)}; continuing snapshots "
            "and waiting for consecutive valid player reads.")

    def _observe_progress(self, snapshot: BotSnapshot) -> bool:
        if (not self._desired_running
                or self.automation_state != AutomationState.RUNNING
                or not snapshot.player_valid or not snapshot.player_fresh
                or snapshot.player is None):
            return True
        now = self._clock()
        player = snapshot.player
        raw_target = snapshot.raw.get("target")
        target_id = (snapshot.target.entity_id if snapshot.target is not None
                     else str(raw_target.get("id"))
                     if isinstance(raw_target, Mapping) and raw_target.get("id") is not None
                     else None)
        distance = (snapshot.target.distance if snapshot.target is not None
                    else raw_target.get("distance")
                    if isinstance(raw_target, Mapping) else None)
        try:
            distance = None if distance is None else float(distance)
        except (TypeError, ValueError):
            distance = None
        progressed = self._progress_player is None
        if self._progress_player is not None:
            progressed = math.hypot(
                player[0] - self._progress_player[0],
                player[1] - self._progress_player[1]) >= 0.5
        if target_id != self._progress_target_id:
            progressed = True
        if (distance is not None and self._progress_target_distance is not None
                and distance <= self._progress_target_distance - 0.5):
            progressed = True
        if progressed:
            self._progress_at = now
            self._progress_player = player
            self._progress_target_id = target_id
            self._progress_target_distance = distance
            return True
        if now - self._progress_at < self._progress_timeout_s:
            return True
        candidates = tuple(entity for entity in snapshot.entities
                           if entity.valid_monster
                           and entity.inside_zone is not False)
        control = snapshot.raw.get("control", {})
        stick = control.get("stick", ()) if isinstance(control, Mapping) else ()
        try:
            moving_command = bool(
                isinstance(stick, (list, tuple)) and len(stick) >= 2
                and (abs(float(stick[0])) > 0.05
                     or abs(float(stick[1])) > 0.05))
        except (TypeError, ValueError) as exc:
            self.fail(FailureCode.MALFORMED_SNAPSHOT,
                      SnapshotError(f"invalid control.stick: {exc}"))
            return False
        if target_id is not None:
            reason = "progress watchdog: target/navigation made no progress"
        elif not candidates:
            reason = "progress watchdog: no eligible target and patrol made no progress"
        elif moving_command:
            reason = "progress watchdog: movement command issued but player did not move"
        else:
            reason = "progress watchdog: neutral output with eligible candidates"
        try:
            self.runtime.pause()
        except Exception as exc:
            self.fail(FailureCode.CONTROLLER_COMMAND, exc)
            return False
        self._begin_recovery(reason, snapshot)
        return True

    def _check_recovery_deadline(self, snapshot: BotSnapshot) -> None:
        if not self._recovering or not self._desired_running:
            return
        now = self._clock()
        elapsed = now - self._recovery_started_at
        if elapsed >= self._recovery_max_s:
            zone = snapshot.zone.name or self._last_options.get("area") or ""
            actions = "; ".join(self._recovery_actions) or "none"
            diagnostic = (
                f"recovery window exhausted after {elapsed:.1f}s; "
                f"state_before=RUNNING trigger={self._recovery_reason!r} "
                f"last_valid_seq={self._last_valid_sequence} "
                f"last_valid_player={self._last_valid_player!r} zone={zone!r} "
                f"snapshot_seq={snapshot.sequence} entities={len(snapshot.entities)} "
                f"candidates={len(snapshot.monsters_in_zone)} "
                f"actions={actions}; exact_reason=recovery remained invalid "
                "through the configured total window")
            self.log("[Recovery abandoned] " + diagnostic)
            self.fail(FailureCode.RECOVERY_EXHAUSTED, RuntimeError(diagnostic))
            return
        if now < self._recovery_next_restart_at:
            return
        restart = getattr(self.runtime, "restart_current", None)
        if restart is None:
            self._recovery_next_restart_at = now + self._recovery_restart_s
            self.log("[Recovery] Runtime has no exact-worker restart command; "
                     "continuing bounded snapshot validation.")
            return
        attempt = 1 + sum(action.startswith("worker restart")
                          for action in self._recovery_actions)
        try:
            restart(self._recovery_reason)
        except Exception as exc:
            action = f"worker restart {attempt} failed: {exc}"
        else:
            action = f"worker restart {attempt} requested"
        self._recovery_actions.append(action)
        delay = min(30.0, self._recovery_restart_s * (2 ** min(attempt, 3)))
        self._recovery_next_restart_at = now + delay
        self.log(f"[Recovery] {action}; next escalation in {delay:.1f}s.")

    def _clear_recovery(self) -> None:
        self._recovering = False
        self._recovery_reason = ""
        self._recovery_started_at = 0.0
        self._recovery_attempts = 0
        self._recovery_valid_streak = 0
        self._recovery_resume_requested = False
        self._recovery_start_sequence = 0
        self._recovery_resume_sequence = 0
        self._recovery_next_restart_at = 0.0
        self._recovery_actions = []
        self._progress_at = self._clock()
        self._progress_player = None
        self._progress_target_id = None
        self._progress_target_distance = None

    def monitor_status(self, state: ConnectionState, message: str) -> None:
        if self._emergency_latched:
            return
        old_connection = self.connection_state
        self.connection_state = ConnectionState(state)
        if self._desired_running:
            if not self._recovering:
                self._begin_recovery(str(message))
            self.automation_state = AutomationState.RECOVERING
            self.state = BotState.RECOVERING
        else:
            self.automation_state = AutomationState.IDLE
            self.state = (BotState.DISCONNECTED
                          if self.connection_state == ConnectionState.DISCONNECTED
                          else BotState.STARTING)
        self.last_error = ""
        self._process_id = 0
        self._session_id = ""
        if self._recovering:
            self._recovery_start_sequence = 0
        self.last_snapshot = BotSnapshot.from_mapping({
            "state": self.state.value,
            "connection_state": self.connection_state.value,
            "automation_state": self.automation_state.value,
            "connected": self.connection_state == ConnectionState.CONNECTED,
            "status": str(message),
        })
        if old_connection != self.connection_state:
            self.log(f"[Connection] old={old_connection.value} "
                     f"new={self.connection_state.value} reason={message}")

    def worker_exited(self, requested: bool, details: str = "") -> None:
        if requested:
            self._worker_started = False
            self.connection_state = ConnectionState.DISCONNECTED
            if not self._emergency_latched:
                self.state = BotState.STOPPED
            return
        if self._desired_running:
            self._worker_started = True
            self.connection_state = ConnectionState.ERROR
            self._begin_recovery(details or "worker exited unexpectedly")
            return
        self.fail(FailureCode.WORKER_STOPPED,
                  RuntimeError(details or "worker exited unexpectedly"))

    def worker_finished(self, result) -> WorkerDisposition:
        disposition = classify_worker_exit(result)
        if getattr(result, "mode_switch", False):
            self._worker_started = True
            self.automation_state = AutomationState.IDLE
            self.state = BotState.SWITCHING_MODE
            self.last_error = ""
            self._process_id = 0
            self._session_id = ""
            self.log("[Mode] Old worker exited normally; starting replacement.")
            return disposition
        if getattr(result, "recovery_restart", False):
            self._worker_started = True
            self.connection_state = ConnectionState.DISCONNECTED
            if self._desired_running:
                self._begin_recovery("worker watchdog/process recovery restart")
            else:
                self.automation_state = AutomationState.IDLE
                self.state = BotState.DISCONNECTED
            self.last_error = ""
            self._process_id = 0
            self._session_id = ""
            if self._recovering:
                self._recovery_start_sequence = 0
            self.log("[Recovery] Old worker exited safely; waiting for replacement snapshots.")
            return disposition
        if disposition == WorkerDisposition.SUCCESS:
            if not self._emergency_latched:
                self.automation_state = AutomationState.IDLE
                self.state = BotState.STOPPED
            self.last_error = ""
            self.log("[Worker] One-shot task completed successfully.")
            return disposition
        if disposition == WorkerDisposition.NORMAL_SHUTDOWN:
            self._worker_started = False
            self.connection_state = ConnectionState.DISCONNECTED
            if not self._emergency_latched:
                self.automation_state = AutomationState.IDLE
                self.state = BotState.STOPPED
            self.last_error = ""
            return disposition
        if disposition == WorkerDisposition.RESTART:
            self._worker_started = True
            self.connection_state = ConnectionState.DISCONNECTED
            if self._desired_running:
                self._begin_recovery("worker ended; waiting to reconnect")
            else:
                self.automation_state = AutomationState.IDLE
                self.state = BotState.DISCONNECTED
            self.last_error = ""
            self.last_snapshot = BotSnapshot.safe(
                BotState.DISCONNECTED, "Waiting for game…")
            self.log("[Monitor] Worker ended; waiting to reconnect.")
            return disposition
        self._worker_started = True
        self.connection_state = ConnectionState.ERROR
        self.last_error = (
            f"worker exited code={result.exit_code} status={result.status_name}")
        if self._desired_running:
            self._begin_recovery(self.last_error)
            self.last_snapshot = BotSnapshot.safe(BotState.RECOVERING,
                                                  self.last_error)
            self.log(f"[Recovery:{FailureCode.WORKER_STOPPED.value}] "
                     f"{self.last_error}; bounded reconnect remains active.")
        else:
            self.automation_state = AutomationState.SAFE_STOP
            self.state = BotState.SAFE_STOP
            self.last_snapshot = BotSnapshot.safe(BotState.SAFE_STOP,
                                                  self.last_error)
            self.log(f"[Error:{FailureCode.WORKER_STOPPED.value}] "
                     f"{self.last_error}; bounded reconnect remains active.")
        return disposition

    def fail(self, code: FailureCode, error: BaseException) -> None:
        policy = FAILURE_POLICIES[code]
        transient = code in (
            FailureCode.MALFORMED_SNAPSHOT,
            FailureCode.CONTROLLER_COMMAND,
            FailureCode.WORKER_STOPPED,
        )
        if transient:
            self.last_error = policy.user_message
            if self._desired_running:
                try:
                    self.runtime.pause()
                except Exception:
                    self.log("[Recovery] Input neutralization command also failed; "
                             "worker heartbeat/reconnect remains armed.")
                self._worker_started = True
                self.connection_state = ConnectionState.ERROR
                self._begin_recovery(f"{policy.user_message}: {error}")
                self.last_snapshot = BotSnapshot.safe(
                    BotState.RECOVERING, self.last_error)
                self.log(f"[Recovery:{code.value}] {policy.user_message}: {error}\n"
                         + "".join(traceback.format_exception(error)))
            else:
                self.log(f"[Monitor:{code.value}] {policy.user_message}: {error}; "
                         "next snapshot/reconnect will retry automatically.")
            return
        self._emergency_latched = True
        try:
            self.runtime.emergency_stop()
        except Exception:
            self.log("[Safety] Emergency release also failed:\n" + traceback.format_exc())
        self._desired_running = False
        self._clear_recovery()
        self._worker_started = False
        self.connection_state = ConnectionState.ERROR
        self.automation_state = AutomationState.SAFE_STOP
        self.state = policy.safe_state
        self.last_error = policy.user_message
        self.last_snapshot = BotSnapshot.safe(policy.safe_state,
                                              policy.user_message)
        self.log(f"[Error:{code.value}] {policy.user_message}: {error}\n"
                 + "".join(traceback.format_exception(error)))
