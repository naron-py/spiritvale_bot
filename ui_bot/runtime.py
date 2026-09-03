"""Asynchronous real and deterministic demo runtimes for the Qt UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from shiboken6 import isValid

from .model import (BotSnapshot, BotState, ConnectionState, FailureCode,
                    SnapshotError)
from .process_discovery import find_spiritvale_pids
from .runtime_child import EVENT_PREFIX, SNAPSHOT_PREFIX


COMMANDS = frozenset(("resume", "pause", "stop", "emergency", "configure",
                      "memory_wait", "memory_recovered", "ping"))


class ZoneRejectionLogLimiter:
    """Suppress normal per-scan rejection churn before it reaches the UI log."""
    pattern = re.compile(r"^zone: rejected (\d+) monster\(s\) outside ")

    def __init__(self, interval_s: float = 15.0):
        self.interval_s = max(1.0, float(interval_s))
        self.previous = None
        self.last_log = 0.0

    def allow(self, message: str, now: float | None = None) -> bool:
        match = self.pattern.match(str(message))
        if match is None:
            return True
        now = time.monotonic() if now is None else float(now)
        current = int(match.group(1))
        previous = self.previous
        material = (previous is None or
                    abs(current - previous) >= max(
                        5, int(max(previous, 1) * 0.10)))
        allowed = material or now - self.last_log >= self.interval_s
        self.previous = current
        if allowed:
            self.last_log = now
        return allowed


class WorkerPurpose(str, Enum):
    MONITOR = "monitor"
    ONE_SHOT = "one_shot"


class WorkerLifetime(str, Enum):
    PERSISTENT = "persistent"
    ONE_SHOT = "one_shot"


class WorkerDisposition(str, Enum):
    SUCCESS = "success"
    NORMAL_SHUTDOWN = "normal_shutdown"
    RESTART = "restart"
    FAILURE = "failure"


@dataclass(frozen=True)
class WorkerExit:
    purpose: WorkerPurpose
    expected_lifetime: WorkerLifetime
    stop_requested: bool
    exit_code: int
    exit_status: object
    last_valid_snapshot_time: float | None = None
    process_gone: bool = False
    generation: int = 0
    mode_switch: bool = False
    recovery_restart: bool = False

    @property
    def status_name(self) -> str:
        return str(getattr(self.exit_status, "name", self.exit_status))


def classify_worker_exit(result: WorkerExit) -> WorkerDisposition:
    normal = result.exit_code == 0 and result.status_name == "NormalExit"
    if not normal:
        return WorkerDisposition.FAILURE
    if result.expected_lifetime == WorkerLifetime.ONE_SHOT:
        return WorkerDisposition.SUCCESS
    if result.stop_requested:
        return WorkerDisposition.NORMAL_SHUTDOWN
    return WorkerDisposition.RESTART


class ReconnectBackoff:
    def __init__(self, initial_ms=1000, maximum_ms=10_000):
        self.initial_ms = max(1, int(initial_ms))
        self.maximum_ms = max(self.initial_ms, int(maximum_ms))
        self._next = self.initial_ms

    def next_delay(self) -> int:
        delay = self._next
        self._next = min(self.maximum_ms, self._next * 2)
        return delay

    def reset(self) -> None:
        self._next = self.initial_ms


def encode_command(command: str, config=None) -> bytes:
    if command not in COMMANDS:
        raise ValueError(f"invalid runtime command {command!r}")
    payload = {"command": command}
    if command == "configure":
        if not isinstance(config, dict):
            raise ValueError("configure requires a controller configuration")
        payload["config"] = config
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def parse_protocol_line(line: str):
    line = line.rstrip("\r\n")
    if line.startswith(SNAPSHOT_PREFIX):
        try:
            raw = json.loads(line[len(SNAPSHOT_PREFIX):])
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"malformed snapshot JSON: {exc}") from exc
        return "snapshot", BotSnapshot.from_mapping(raw)
    if line.startswith(EVENT_PREFIX):
        try:
            raw = json.loads(line[len(EVENT_PREFIX):])
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"malformed event JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SnapshotError("runtime event must be an object")
        return "event", raw
    return "log", line


class RuntimeSignals(QObject):
    snapshot = Signal(object)
    event = Signal(str)
    failure = Signal(object, object)
    exited = Signal(bool, str)
    worker_finished = Signal(object)
    monitor_status = Signal(object, str)


class ProcessRuntime(QObject):
    """QProcess supervisor; no pipe read or wait blocks the UI event loop."""

    def __init__(self, project_root: str | Path, parent=None,
                 child_module="ui_bot.runtime_child", process_finder=None,
                 retry_min_ms=1000, retry_max_ms=10_000,
                 snapshot_stale_ms=8000, watchdog_check_ms=1000,
                 heartbeat_timeout_ms=2000):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.child_module = str(child_module)
        self.signals = RuntimeSignals()
        self.process: QProcess | None = None
        self._buffer = b""
        self._requested_stop = False
        self._auto_resume = False
        self._emergency = False
        self.process_finder: Callable[[], list[int]] = (
            process_finder or find_spiritvale_pids)
        self.backoff = ReconnectBackoff(retry_min_ms, retry_max_ms)
        self.worker_purpose = WorkerPurpose.MONITOR
        self.expected_lifetime = WorkerLifetime.PERSISTENT
        self.stop_requested = False
        self.last_exit_code: int | None = None
        self.last_exit_status: object | None = None
        self.last_valid_snapshot_time: float | None = None
        self.last_snapshot_time: float | None = None
        self.last_heartbeat_time: float | None = None
        self.child_alive = False
        self.pipe_alive = False
        self.monitor_loop_alive = False
        self.last_player_read_time: float | None = None
        self.last_entity_scan_time: float | None = None
        self.scan_in_progress = False
        self.scan_started_at = 0.0
        self._last_player_read_version = 0
        self._last_scan_version = 0
        self._heartbeat_pending_at: float | None = None
        self._heartbeat_snapshot_time: float | None = None
        self._last_heartbeat_probe = 0.0
        self._heartbeat_monitor_alive = True
        self.zone_log_limiter = ZoneRejectionLogLimiter()
        self.snapshot_stale_ms = max(100, int(snapshot_stale_ms))
        self.heartbeat_timeout_ms = max(20, int(heartbeat_timeout_ms))
        self.current_pid: int | None = None
        self.session_id = ""
        self._session_generation = 0
        self._supervising = False
        self._options = {}
        self._restart_after_stop = False
        self._resume_after_restart = False
        self._active_generation = 0
        self._switching_mode = False
        self._intentional_stop = False
        self._pending_switch_options = None
        self.discovery_timer = QTimer(self)
        self.discovery_timer.setSingleShot(True)
        self.discovery_timer.timeout.connect(self.discover_now)
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(max(20, int(watchdog_check_ms)))
        self.watchdog_timer.timeout.connect(self._check_snapshot_heartbeat)

    def snapshot_belongs_to_current_session(self, snapshot: BotSnapshot) -> bool:
        return bool(
            self.current_pid is not None
            and snapshot.process_id == self.current_pid
            and self.session_id
            and snapshot.session_id == self.session_id
        )

    @property
    def monitoring(self):
        return self._supervising

    @property
    def active_generation(self):
        return self._active_generation

    @property
    def switching_mode(self):
        return self._switching_mode

    def attach(self, options):
        if self._supervising:
            raise RuntimeError("runtime supervisor is already active")
        self._supervising = True
        self._options = dict(options)
        self._requested_stop = False
        self.stop_requested = False
        self._emergency = False
        self._auto_resume = False
        self._switching_mode = False
        self._intentional_stop = False
        self._pending_switch_options = None
        self.backoff.reset()
        self.watchdog_timer.start()
        self.discover_now()

    def discover_now(self):
        if (not self._supervising or self._switching_mode
                or self._intentional_stop):
            return
        try:
            pids = sorted({int(pid) for pid in self.process_finder()
                           if int(pid) > 0})
        except Exception as exc:
            self.signals.event.emit(f"[Discovery] Process lookup failed: {exc}")
            self.signals.monitor_status.emit(
                ConnectionState.DISCONNECTED, f"Waiting for game… ({exc})")
            self._schedule_discovery()
            return
        if self.process is not None:
            if self.current_pid in pids:
                self._schedule_discovery(self.backoff.maximum_ms)
                return
            self.signals.event.emit(
                "[Discovery] Game process closed; detaching safely.")
            self._requested_stop = True
            self.stop_requested = True
            self._intentional_stop = True
            self._restart_after_stop = True
            self._auto_resume = False
            try:
                self._send("emergency")
            except RuntimeError:
                self._kill_process(self.process, self._active_generation)
            return
        if not pids:
            self.current_pid = None
            self.session_id = ""
            self.last_valid_snapshot_time = None
            self.signals.monitor_status.emit(
                ConnectionState.DISCONNECTED, "Waiting for game…")
            self._schedule_discovery()
            return
        if len(pids) > 1:
            self.signals.event.emit(
                f"[Discovery] Multiple SpiritVale processes found; attaching to PID {pids[0]}.")
        self._launch_monitor(pids[0])

    def _schedule_discovery(self, delay_ms=None):
        if (self._supervising and not self._switching_mode
                and not self._intentional_stop):
            delay = self.backoff.next_delay() if delay_ms is None else int(delay_ms)
            self.discovery_timer.start(max(1, delay))

    def _launch_monitor(self, pid):
        if self.process is not None:
            raise RuntimeError("cannot launch replacement before old worker cleanup")
        self.discovery_timer.stop()
        process = QProcess(self)
        process.setProgram(sys.executable)
        self._session_generation += 1
        generation = self._session_generation
        self._active_generation = generation
        process.setProperty("worker_generation", generation)
        self.current_pid = int(pid)
        self.session_id = f"{self.current_pid}:{self._session_generation}"
        self.last_valid_snapshot_time = None
        self.last_snapshot_time = time.monotonic()
        self.last_heartbeat_time = None
        self.last_player_read_time = None
        self.last_entity_scan_time = None
        self.scan_in_progress = False
        self.scan_started_at = 0.0
        self._last_player_read_version = 0
        self._last_scan_version = 0
        self._heartbeat_pending_at = None
        self._heartbeat_snapshot_time = None
        self._last_heartbeat_probe = 0.0
        self._heartbeat_monitor_alive = True
        self.zone_log_limiter = ZoneRejectionLogLimiter()
        options = self._options
        args = ["-u", "-m", self.child_module,
                "--expected-pid", str(self.current_pid),
                "--session-id", self.session_id,
                "--mode", str(options.get("mode", "memory")),
                "--max-entities", str(int(options.get("max_entities", 250))),
                "--trail-length", str(int(options.get("trail_length", 120)))]
        area = (str(options.get("area", "")).strip()
                if str(options.get("mode", "memory")) == "memory" else "")
        if area:
            args += ["--area", area]
        if not options.get("auto_reconnect", True):
            args.append("--no-reconnect")
        control_config = options.get("control_config")
        if isinstance(control_config, dict):
            args += ["--control-config", json.dumps(
                control_config, separators=(",", ":"))]
        process.setArguments(args)
        process.setWorkingDirectory(str(self.project_root))
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.started.connect(self._started)
        process.readyReadStandardOutput.connect(self._read_output)
        process.errorOccurred.connect(self._process_error)
        process.finished.connect(self._finished)
        self.process = process
        self._buffer = b""
        self._requested_stop = False
        self.stop_requested = False
        self._emergency = False
        self._restart_after_stop = False
        if not self._switching_mode:
            self.signals.monitor_status.emit(
                ConnectionState.CONNECTING,
                f"Attaching to SpiritVale PID {self.current_pid}…")
        process.start()

    def start(self, options):
        self._auto_resume = True
        self.attach(options)
        self._auto_resume = True

    def _started(self, process=None, generation=None):
        if process is None:
            process = self.sender()
        if generation is None and process is not None:
            generation = int(process.property("worker_generation"))
        if process is not None and (process is not self.process
                                    or generation != self._active_generation):
            return
        self._switching_mode = False
        self._intentional_stop = False
        self.child_alive = True
        self.signals.event.emit("[Runtime] Bot worker process started.")
        self.last_snapshot_time = time.monotonic()
        if self._auto_resume:
            self._send("resume")

    def resume(self):
        self._auto_resume = True
        self._send("resume")

    def select_mode(self, options):
        """Restart a paused monitor when its targeting configuration changed."""
        desired = str(options.get("mode", "memory"))
        current = str(self._options.get("mode", "memory"))
        if self._switching_mode:
            self._pending_switch_options = dict(options)
            return True
        def targeting_config(values):
            mode = str(values.get("mode", "memory"))
            area = (str(values.get("area", "")).strip()
                    if mode == "memory" else "")
            revision = values.get("area_revision", 0) if area else 0
            return mode, area, revision

        changed = targeting_config(options) != targeting_config(self._options)
        if not changed or self.process is None:
            self._options = dict(options)
            return False
        self.signals.event.emit(
            f"[Mode] Restarting paused worker configuration: "
            f"{current} -> {desired}.")
        self._switching_mode = True
        self._intentional_stop = True
        self._pending_switch_options = dict(options)
        self._requested_stop = True
        self.stop_requested = True
        self._auto_resume = False
        self._send("emergency")
        self._schedule_process_kill(self.process, self._active_generation)
        return True

    def pause(self):
        self._auto_resume = False
        self._send("pause")

    def wait_for_memory(self):
        self._send("memory_wait")

    def memory_recovered(self):
        self._send("memory_recovered")

    def update_controller_config(self, config):
        config = dict(config)
        self._options["control_config"] = config
        if self._pending_switch_options is not None:
            pending = dict(self._pending_switch_options)
            pending["control_config"] = config
            self._pending_switch_options = pending
        if self.process is not None and not self._switching_mode:
            self._send("configure", config)

    def stop(self):
        if self.process is None:
            return
        if self._switching_mode:
            self._options = dict(self._pending_switch_options or self._options)
            self._pending_switch_options = None
            self._switching_mode = False
            self._intentional_stop = True
            self._requested_stop = True
            self.stop_requested = True
            self._restart_after_stop = True
            self._auto_resume = False
            self._send("emergency")
            self._schedule_process_kill(self.process, self._active_generation)
            return
        self._auto_resume = False
        self._send("pause")

    def emergency_stop(self):
        self._supervising = False
        self.discovery_timer.stop()
        self.watchdog_timer.stop()
        self._switching_mode = False
        self._pending_switch_options = None
        self._intentional_stop = True
        if self.process is None:
            return
        self._requested_stop = True
        self.stop_requested = True
        self._emergency = True
        self._auto_resume = False
        self._send("emergency")
        # Give the unchanged loop time to reach its finally: pad.close(). A hard
        # kill is fallback only, never the first release mechanism.
        process = self.process
        generation = self._active_generation
        self._schedule_process_kill(process, generation)

    def reset_emergency(self):
        self._emergency = False

    def _send(self, command: str, config=None):
        process = self.process
        if process is None or process.state() == QProcess.NotRunning:
            raise RuntimeError("bot worker is not running")
        written = process.write(encode_command(command, config))
        if written < 0:
            raise RuntimeError(f"could not send {command} to bot worker")

    def _read_output(self, process=None, generation=None):
        if process is None:
            process = self.sender()
        if generation is None and process is not None:
            generation = int(process.property("worker_generation"))
        if process is None:
            return
        output = bytes(process.readAllStandardOutput())
        if (process is not self.process
                or (generation is not None
                    and generation != self._active_generation)):
            self.signals.event.emit(
                "[Runtime] Ignored output from an old worker generation.")
            return
        self._buffer += output
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if not text:
                continue
            if not self.zone_log_limiter.allow(text):
                continue
            try:
                kind, payload = parse_protocol_line(text)
                if kind == "snapshot":
                    if not self.snapshot_belongs_to_current_session(payload):
                        self.signals.event.emit(
                            "[Runtime] Discarded snapshot from an old process session.")
                        continue
                    received_at = time.monotonic()
                    self.last_snapshot_time = received_at
                    self.child_alive = True
                    self.pipe_alive = True
                    self.monitor_loop_alive = True
                    self._heartbeat_pending_at = None
                    self._heartbeat_snapshot_time = None
                    self.scan_in_progress = payload.scan_in_progress
                    self.scan_started_at = payload.scan_started_at
                    if payload.player_read_version > self._last_player_read_version:
                        self._last_player_read_version = payload.player_read_version
                        self.last_player_read_time = (
                            payload.player_read_at or time.time())
                    if payload.scan_version > self._last_scan_version:
                        self._last_scan_version = payload.scan_version
                        self.last_entity_scan_time = (
                            payload.last_scan_completed_at or time.time())
                    if (payload.connection_state == ConnectionState.CONNECTED
                            and payload.player_fresh and payload.player is not None):
                        self.last_valid_snapshot_time = time.monotonic()
                        self.backoff.reset()
                    self.signals.snapshot.emit(payload)
                elif kind == "event":
                    if payload.get("type") == "heartbeat":
                        self.last_heartbeat_time = time.monotonic()
                        self.child_alive = True
                        self.pipe_alive = True
                        self._heartbeat_monitor_alive = bool(
                            payload.get("monitor_loop_alive", True))
                        self.monitor_loop_alive = self._heartbeat_monitor_alive
                        self.scan_in_progress = bool(
                            payload.get("scan_in_progress", self.scan_in_progress))
                        self.scan_started_at = float(
                            payload.get("scan_started_at", self.scan_started_at) or 0.0)
                        player_at = float(payload.get("last_player_read", 0.0) or 0.0)
                        scan_at = float(payload.get("last_entity_scan", 0.0) or 0.0)
                        if player_at:
                            self.last_player_read_time = player_at
                        if scan_at:
                            self.last_entity_scan_time = scan_at
                        continue
                    message = str(payload.get("message", "runtime event"))
                    self.signals.event.emit(f"[{payload.get('level', 'INFO')}] {message}")
                else:
                    self.signals.event.emit(payload)
            except SnapshotError as exc:
                self.signals.failure.emit(FailureCode.MALFORMED_SNAPSHOT, exc)

    def _check_snapshot_heartbeat(self):
        process = self.process
        if (not self._supervising or process is None
                or self._requested_stop or self._intentional_stop
                or self._switching_mode or self.last_snapshot_time is None):
            return
        self.child_alive = process.state() != QProcess.NotRunning
        if not self.child_alive:
            return
        now = time.monotonic()
        stale_ms = (now - self.last_snapshot_time) * 1000.0
        if stale_ms < self.snapshot_stale_ms:
            self._heartbeat_pending_at = None
            self._heartbeat_snapshot_time = None
            return
        if self._heartbeat_pending_at is None:
            if (now - self._last_heartbeat_probe) * 1000.0 < self.snapshot_stale_ms:
                return
            self._heartbeat_pending_at = now
            self._heartbeat_snapshot_time = self.last_snapshot_time
            self._last_heartbeat_probe = now
            try:
                self._send("ping")
            except RuntimeError:
                if self.last_snapshot_time != self._heartbeat_snapshot_time:
                    self._heartbeat_pending_at = None
                    self._heartbeat_snapshot_time = None
                    return
                self.restart_current("worker pipe unavailable during heartbeat probe")
            else:
                self.signals.event.emit(
                    f"[Decision] condition=stale_dashboard action=wait "
                    f"reason=heartbeat probe sent after {stale_ms / 1000.0:.1f}s")
            return
        if self.last_snapshot_time != self._heartbeat_snapshot_time:
            self._heartbeat_pending_at = None
            self._heartbeat_snapshot_time = None
            self.signals.event.emit(
                "[Decision] condition=stale_dashboard action=continue "
                "reason=fresh snapshot arrived during heartbeat check")
            return
        heartbeat_replied = bool(
            self.last_heartbeat_time is not None
            and self.last_heartbeat_time >= self._heartbeat_pending_at)
        if heartbeat_replied and self._heartbeat_monitor_alive:
            self._heartbeat_pending_at = None
            self._heartbeat_snapshot_time = None
            action = "wait" if self.scan_in_progress else "continue"
            reason = ("entity scan still in progress" if self.scan_in_progress
                      else "child process, pipe, and monitor heartbeat are alive")
            self.signals.event.emit(
                f"[Decision] condition=stale_dashboard action={action} reason={reason}")
            return
        if (now - self._heartbeat_pending_at) * 1000.0 < self.heartbeat_timeout_ms:
            return
        # Re-check the snapshot marker immediately before retiring this exact
        # generation; readyRead can race the watchdog timer callback.
        if self.last_snapshot_time != self._heartbeat_snapshot_time:
            self._heartbeat_pending_at = None
            self._heartbeat_snapshot_time = None
            return
        reason = ("monitor loop heartbeat confirms a hang"
                  if heartbeat_replied else "worker pipe did not answer heartbeat")
        if not heartbeat_replied:
            self.pipe_alive = False
        self.monitor_loop_alive = False
        self.signals.event.emit(
            f"[Decision] condition=stale_dashboard action=stop reason={reason}")
        self.restart_current(reason)

    def restart_current(self, reason: str) -> None:
        """Retire exactly the owned worker and reconnect after its finished signal."""
        process = self.process
        if process is None:
            if self._supervising:
                self._schedule_discovery()
            return
        if self._requested_stop or self._intentional_stop or self._switching_mode:
            return
        generation = self._active_generation
        self.signals.event.emit(
            f"[Watchdog] {reason}; restarting worker generation {generation}.")
        self._requested_stop = True
        self.stop_requested = True
        self._intentional_stop = True
        self._restart_after_stop = True
        self._auto_resume = False
        try:
            self._send("emergency")
        except RuntimeError:
            self._kill_process(process, generation)
        else:
            self._schedule_process_kill(process, generation)

    def _process_error(self, error, process=None, generation=None):
        if process is None:
            process = self.sender()
        if generation is None and process is not None:
            generation = int(process.property("worker_generation"))
        if (process is not self.process
                or (generation is not None
                    and generation != self._active_generation)):
            return
        if self._requested_stop or self._intentional_stop:
            return
        message = process.errorString() if process else str(error)
        failed_to_start = error == QProcess.ProcessError.FailedToStart
        if failed_to_start:
            self.process = None
            self.child_alive = False
            self.pipe_alive = False
            self.monitor_loop_alive = False
            self._buffer = b""
            if process is not None:
                process.deleteLater()
            result = WorkerExit(
                self.worker_purpose, self.expected_lifetime, False,
                -1, "CrashExit", self.last_valid_snapshot_time,
                generation=int(generation or self._active_generation))
            if self._supervising:
                self.current_pid = None
                self.signals.monitor_status.emit(
                    ConnectionState.DISCONNECTED, "Waiting for game…")
                self._schedule_discovery()
            self.signals.worker_finished.emit(result)
        else:
            self.signals.event.emit(f"[Runtime] Process error: {message}")

    def _finished(self, exit_code: int, exit_status, process=None,
                  generation=None):
        if process is None:
            process = self.sender()
        if generation is None and process is not None:
            generation = int(process.property("worker_generation"))
        generation = (self._active_generation if generation is None
                      else int(generation))
        if process is not self.process or generation != self._active_generation:
            self.signals.event.emit(
                f"[Runtime] Ignored exit from old worker generation {generation}.")
            return
        if self._buffer:
            text = self._buffer.decode("utf-8", errors="replace").strip()
            if text:
                self.signals.event.emit(text)
        requested = bool(self._requested_stop or self._intentional_stop)
        mode_switch_requested = bool(
            self._switching_mode and self._intentional_stop)
        process_gone = False
        pid = self.current_pid
        try:
            process_gone = (pid is not None and pid not in self.process_finder())
        except Exception:
            pass
        status_name = getattr(exit_status, "name", str(exit_status))
        normal_exit = int(exit_code) == 0 and status_name == "NormalExit"
        mode_switch = bool(mode_switch_requested and normal_exit)
        reconnect_after_stop = bool(
            (self._restart_after_stop and not mode_switch)
            or (mode_switch_requested and not normal_exit))
        details = f"worker exited code={exit_code} status={status_name}"
        result = WorkerExit(
            self.worker_purpose, self.expected_lifetime, requested,
            int(exit_code), exit_status, self.last_valid_snapshot_time,
            process_gone=process_gone, generation=generation,
            mode_switch=mode_switch, recovery_restart=reconnect_after_stop)
        disposition = classify_worker_exit(result)
        self.last_exit_code = int(exit_code)
        self.last_exit_status = exit_status

        # The exact process that emitted finished is fully detached before a
        # replacement is assigned to self.process.
        self.process = None
        self.child_alive = False
        self.pipe_alive = False
        self.monitor_loop_alive = False
        self._buffer = b""
        self.session_id = ""
        self._restart_after_stop = False
        self._resume_after_restart = False
        process.deleteLater()

        if mode_switch:
            options = dict(self._pending_switch_options or self._options)
            self._pending_switch_options = None
            self._options = options
            self._requested_stop = False
            self.stop_requested = False
            self._auto_resume = True
            self.signals.worker_finished.emit(result)
            self._launch_monitor(pid)
            return

        if mode_switch_requested:
            # Preserve the requested configuration, but a crash/forced exit must
            # pass through bounded discovery and recovery rather than being
            # declared a successful switch.
            self._options = dict(self._pending_switch_options or self._options)
            self._pending_switch_options = None
            self._switching_mode = False

        self._intentional_stop = False
        self.current_pid = None
        should_reconnect = bool(
            self._supervising and (reconnect_after_stop or disposition in (
                WorkerDisposition.RESTART, WorkerDisposition.FAILURE)))
        if should_reconnect:
            self._auto_resume = False
            self.signals.monitor_status.emit(
                ConnectionState.DISCONNECTED, "Waiting for game…")
            self._schedule_discovery()
        else:
            self._supervising = False
            self.discovery_timer.stop()
        self.signals.worker_finished.emit(result)
        if requested and not reconnect_after_stop:
            self.signals.exited.emit(True, details)

    def _kill_process(self, process, generation):
        if process is None or not isValid(process):
            return
        try:
            running = process.state() != QProcess.NotRunning
        except RuntimeError:
            # deleteLater() already completed for this captured generation.
            return
        if running:
            self.signals.event.emit(
                f"[Safety] Worker generation {generation} did not exit; terminating.")
            process.terminate()
            if not process.waitForFinished(500):
                process.kill()

    def _schedule_process_kill(self, process, generation, delay_ms=1500):
        if process is None or not isValid(process):
            return
        timer = QTimer(process)
        timer.setSingleShot(True)
        timer.setProperty("worker_generation", generation)
        timer.timeout.connect(self._kill_timer_fired)
        timer.start(max(1, int(delay_ms)))

    def _kill_timer_fired(self):
        timer = self.sender()
        if timer is None or not isValid(timer):
            return
        self._kill_process(timer.parent(), int(timer.property("worker_generation")))

    def shutdown(self, timeout_ms: int = 3500):
        self._supervising = False
        self.discovery_timer.stop()
        self.watchdog_timer.stop()
        process = self.process
        if process is None:
            return
        self._requested_stop = True
        self.stop_requested = True
        try:
            self._send("emergency")
        except RuntimeError:
            pass
        if not process.waitForFinished(timeout_ms):
            process.kill()
            process.waitForFinished(500)


class DemoEngine:
    """Deterministic synthetic world that exercises every visualization layer."""

    def __init__(self, seed: int = 42, trail_length: int = 120,
                 mode: str = "memory"):
        self.seed = int(seed)
        self.frame = 0
        self.sequence = 0
        self.x, self.z = 117.0, 215.0
        self.trail: list[tuple[float, float]] = []
        self.trail_length = trail_length
        self.mode = ("pixel" if str(mode).lower() in ("pixel", "minimap")
                     else "memory")

    def next_snapshot(self, running: bool) -> BotSnapshot:
        if running:
            self.frame += 1
            phase = self.frame * 0.035
            self.x += math.cos(phase) * 0.22
            self.z += math.sin(phase) * 0.18
            self.trail.append((self.x, self.z))
            self.trail = self.trail[-self.trail_length:]
        self.sequence += 1
        monsters = [
            (160.0, 205.0, "Monster #184", True, True),
            (150.0, 222.0, "Mossfang", True, False),
            (128.0, 190.0, "Slime", True, False),
            (66.0, 224.0, "Filtered", False, False),
            (208.0, 170.0, "Ignored", False, False),
        ]
        entities = []
        for index, (x, z, name, valid, current) in enumerate(monsters):
            entities.append({"id": str(184 + index), "kind": "monster",
                             "x": x, "z": z, "name": name,
                             "valid": valid, "ignored": not valid,
                             "current": current,
                             "distance": math.hypot(x - self.x, z - self.z)})
        target = dict(entities[0])
        raw = {
            "sequence": self.sequence, "timestamp": float(self.sequence),
            "state": "RUNNING" if running else "PAUSED",
            "connection_state": "CONNECTED",
            "automation_state": "RUNNING" if running else "IDLE",
            "connected": True, "memory_active": True, "memory_ready": True,
            "pixel_ready": True, "pixel_error": "",
            "source": "pixels" if self.mode == "pixel" else "memory",
            "active_mode": self.mode if running else "waiting",
            "bot_state": "MOVING TO TARGET" if running else "PAUSED",
            "player": {"x": self.x, "z": self.z}, "target": target,
            "player_fresh": True, "scan_version": self.sequence,
            "entities": entities,
            "zone": {"name": "Demo Polygon", "kind": "polygon",
                     "points": [[86, 235], [142, 246], [192, 208],
                                [176, 178], [80, 180]],
                     "safety_margin": 5.0, "auto_return": True},
            "path": [[self.x, self.z], [160.0, 205.0]],
            "trail": self.trail,
            "log": ["[Demo] Deterministic snapshot",
                    "[Memory] 5 entities updated"],
            "status": "Demo data — no game or controller" if running
                      else "Demo paused — controls neutral",
        }
        return BotSnapshot.from_mapping(raw)


class DemoRuntime(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.signals = RuntimeSignals()
        self.engine = DemoEngine()
        self.timer = QTimer(self)
        self.timer.setInterval(67)
        self.timer.timeout.connect(self._tick)
        self.running = False
        self.started = False
        self.control_config = {}

    @property
    def monitoring(self):
        return self.started

    def attach(self, options):
        if self.started:
            raise RuntimeError("demo monitor already active")
        self.engine = DemoEngine(42, int(options.get("trail_length", 120)),
                                 str(options.get("mode", "memory")))
        self.started = True
        self.running = False
        self.control_config = dict(options.get("control_config") or {})
        self.timer.start()
        self.signals.event.emit("[Demo] Read-only demo monitor attached.")
        self._tick()

    def start(self, options):
        self.attach(options)
        self.running = True
        self.signals.event.emit("[Demo] Started deterministic demo mode.")
        self._tick()

    def resume(self):
        if not self.started:
            raise RuntimeError("demo worker is not active")
        self.running = True
        self.timer.start()
        self._tick()

    def select_mode(self, options):
        self.engine.mode = (
            "pixel" if str(options.get("mode", "memory")).lower()
            in ("pixel", "minimap") else "memory")
        return False

    def update_controller_config(self, config):
        self.control_config = dict(config)

    def pause(self):
        if not self.started:
            raise RuntimeError("demo worker is not active")
        self.running = False
        self._tick()

    def stop(self):
        self.running = False
        self._tick()
        self.signals.event.emit(
            "[Demo] Automation stopped; read-only monitoring continues.")

    def emergency_stop(self):
        self.running = self.started = False
        self.timer.stop()
        self.signals.snapshot.emit(BotSnapshot.safe(
            BotState.EMERGENCY_STOP, "Demo emergency stop"))
        self.signals.exited.emit(True, "demo emergency stop")

    def reset_emergency(self):
        pass

    def shutdown(self, timeout_ms=0):
        self.emergency_stop()

    def _tick(self):
        if self.started:
            self.signals.snapshot.emit(self.engine.next_snapshot(self.running))
