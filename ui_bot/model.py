"""Immutable UI snapshots and explicit fail-safe policy definitions.

This module has no Qt dependency so worker boundaries and malformed data can be
tested without creating a GUI or opening the game.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace as field_replace
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

from .readiness import normalize_mode


MAX_WORLD_COORDINATE = 10_000_000.0


class SnapshotError(ValueError):
    pass


class BotState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    SWITCHING_MODE = "SWITCHING_MODE"
    RUNNING = "RUNNING"
    RECOVERING = "RECOVERING"
    PAUSED = "PAUSED"
    SAFE_STOP = "SAFE_STOP"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class AutomationState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    RECOVERING = "RECOVERING"
    PAUSED = "PAUSED"
    SAFE_STOP = "SAFE_STOP"


class ZoneRecordingState(str, Enum):
    INACTIVE = "INACTIVE"
    RECORDING = "RECORDING"
    READY = "READY"
    INVALID = "INVALID"


class ZoneDisplayState(str, Enum):
    NO_SAVED_ZONE = "NO_SAVED_ZONE"
    LOADED_DISCONNECTED = "LOADED_DISCONNECTED"
    ACTIVE = "ACTIVE"
    INVALID = "INVALID"


class FailureCode(str, Enum):
    GAME_NOT_FOUND = "game_not_found"
    MULTIPLE_PROCESSES = "multiple_processes"
    ACCESS_DENIED = "access_denied"
    GAME_RESTARTED = "game_restarted"
    PID_CHANGED = "pid_changed"
    INVALID_HANDLE = "invalid_handle"
    MODULE_UNAVAILABLE = "module_unavailable"
    POINTER_CHAIN = "pointer_chain_failure"
    NULL_POINTER = "null_or_stale_pointer"
    PARTIAL_READ = "partial_memory_read"
    INVALID_COORDINATE = "invalid_coordinate"
    OFFSETS_INVALID = "offsets_invalid"
    ENTITY_RACE = "entity_list_changed"
    PLAYER_UNAVAILABLE = "player_unavailable"
    NO_MONSTERS = "no_monsters"
    TARGET_DESPAWNED = "target_despawned"
    ENTITY_REUSED = "entity_reused"
    TARGET_LEFT_ZONE = "target_left_zone"
    PLAYER_LEFT_ZONE = "player_left_zone"
    EMPTY_POLYGON = "empty_polygon"
    TOO_FEW_POINTS = "too_few_polygon_points"
    SELF_INTERSECTING = "self_intersecting_polygon"
    INVALID_MARGIN = "invalid_safety_margin"
    DESTINATION_OUTSIDE = "destination_outside_zone"
    PLAYER_STUCK = "player_stuck"
    TARGET_UNREACHABLE = "target_unreachable"
    MOVEMENT_TIMEOUT = "movement_timeout"
    CONTROLLER_DISCONNECTED = "controller_disconnected"
    CONTROLLER_COMMAND = "controller_command_failed"
    INPUT_HELD = "movement_input_held"
    INTERRUPTED_ACTION = "pause_or_stop_during_action"
    WORKER_EXCEPTION = "worker_exception"
    WORKER_STOPPED = "worker_stopped_unexpectedly"
    RECOVERY_EXHAUSTED = "recovery_window_exhausted"
    LATE_SIGNAL = "signal_after_widget_destruction"
    DUPLICATE_START = "duplicate_start"
    STOP_BEFORE_START = "stop_before_start"
    WINDOW_CLOSE = "window_closed_while_running"
    FORCE_CLOSE = "forced_close"
    MALFORMED_SNAPSHOT = "malformed_snapshot"
    LOG_OVERFLOW = "excessive_log_volume"
    CONFIG_MISSING = "config_missing"
    CONFIG_CORRUPT = "config_corrupt"
    CONFIG_READ_ONLY = "config_read_only"
    CONFIG_INVALID = "config_invalid"


@dataclass(frozen=True)
class FailurePolicy:
    detection: str
    user_message: str
    safe_state: BotState
    safe_action: str
    retry_policy: str
    logged: str


def _policy(detection: str, message: str,
            state: BotState = BotState.SAFE_STOP,
            retry: str = "No automatic retry") -> FailurePolicy:
    return FailurePolicy(
        detection=detection,
        user_message=message,
        safe_state=state,
        safe_action="Release all movement, attack, and skill inputs; clear target",
        retry_policy=retry,
        logged="Failure code, context, exception, and full traceback",
    )


# This table is deliberately explicit: the UI documentation renders it and tests
# require every enumerated failure to have a safe action and bounded retry policy.
FAILURE_POLICIES = MappingProxyType({
    FailureCode.GAME_NOT_FOUND: _policy("process lookup returns none", "SpiritVale is not running.", BotState.DISCONNECTED, "Manual reconnect or bounded auto reconnect"),
    FailureCode.MULTIPLE_PROCESSES: _policy("process lookup returns multiple matches", "Multiple SpiritVale processes were found; select one explicitly.", BotState.DISCONNECTED),
    FailureCode.ACCESS_DENIED: _policy("OpenProcess raises access denied", "Game memory access was denied.", BotState.DISCONNECTED),
    FailureCode.GAME_RESTARTED: _policy("process exits while attached", "The game closed or restarted.", BotState.DISCONNECTED, "Bounded auto reconnect when enabled"),
    FailureCode.PID_CHANGED: _policy("attached PID differs from current PID", "The game process changed; a fresh connection is required.", BotState.DISCONNECTED),
    FailureCode.INVALID_HANDLE: _policy("read reports an invalid process handle", "The game connection became invalid.", BotState.DISCONNECTED, "One fresh reconnect attempt when enabled"),
    FailureCode.MODULE_UNAVAILABLE: _policy("GameAssembly base lookup fails", "GameAssembly is not available yet.", BotState.DISCONNECTED, "Bounded delayed retry"),
    FailureCode.POINTER_CHAIN: _policy("required pointer walk fails", "A required game object could not be read.", retry="Three delayed read retries, then safe stop"),
    FailureCode.NULL_POINTER: _policy("required pointer is zero or stale", "Game data is temporarily unavailable.", retry="Three delayed read retries, then safe stop"),
    FailureCode.PARTIAL_READ: _policy("read length differs from requested length", "The game returned incomplete memory data.", retry="Three delayed read retries, then safe stop"),
    FailureCode.INVALID_COORDINATE: _policy("coordinate is non-finite or outside bounds", "Invalid world coordinates were rejected."),
    FailureCode.OFFSETS_INVALID: _policy("class validation or structural sanity fails", "Memory offsets no longer match this game version."),
    FailureCode.ENTITY_RACE: _policy("entity list changes during coherent read", "The entity list changed while being read.", BotState.PAUSED, "Retry next bounded scan generation"),
    FailureCode.PLAYER_UNAVAILABLE: _policy("fresh player position is unavailable", "Player data is temporarily unavailable.", BotState.PAUSED, "Three fresh-snapshot retries, then safe stop"),
    FailureCode.NO_MONSTERS: _policy("valid target set is empty", "No valid monsters are currently available.", BotState.PAUSED, "Continue observation without movement"),
    FailureCode.TARGET_DESPAWNED: _policy("held target disappears or dies", "The current target is gone.", BotState.PAUSED, "Clear target and select from next fresh snapshot"),
    FailureCode.ENTITY_REUSED: _policy("stable ID changes at reused pointer", "A recycled entity pointer was detected.", BotState.PAUSED, "Treat as a new entity on next snapshot"),
    FailureCode.TARGET_LEFT_ZONE: _policy("target fails exact zone admission", "The target left the farming zone.", BotState.PAUSED, "Clear target; choose an in-zone target"),
    FailureCode.PLAYER_LEFT_ZONE: _policy("player is outside safe zone interior", "Player left the safe zone; returning is required.", BotState.PAUSED, "Resume only with a valid routed return command"),
    FailureCode.EMPTY_POLYGON: _policy("zone has no points", "The farming polygon is empty.", BotState.PAUSED),
    FailureCode.TOO_FEW_POINTS: _policy("zone has fewer than three unique points", "A polygon needs at least three different points.", BotState.PAUSED),
    FailureCode.SELF_INTERSECTING: _policy("non-adjacent polygon edges intersect", "The polygon crosses itself and cannot be saved.", BotState.PAUSED),
    FailureCode.INVALID_MARGIN: _policy("margin is non-finite, negative, or collapses zone", "The safety margin is invalid.", BotState.PAUSED),
    FailureCode.DESTINATION_OUTSIDE: _policy("final projected segment leaves safe zone", "Unsafe movement outside the zone was blocked.", BotState.PAUSED),
    FailureCode.PLAYER_STUCK: _policy("progress sensors report no useful movement", "The player appears stuck.", BotState.PAUSED, "Bounded alternating escape attempts"),
    FailureCode.TARGET_UNREACHABLE: _policy("router proves a sealed dead end", "The current target is unreachable.", BotState.PAUSED, "Temporarily blacklist target"),
    FailureCode.MOVEMENT_TIMEOUT: _policy("movement or combat budget expires", "The action timed out.", BotState.PAUSED, "Temporarily blacklist target; no endless retry"),
    FailureCode.CONTROLLER_DISCONNECTED: _policy("controller backend reports disconnect", "The virtual controller disconnected."),
    FailureCode.CONTROLLER_COMMAND: _policy("controller command raises or times out", "A controller command failed."),
    FailureCode.INPUT_HELD: _policy("release verification detects non-neutral state", "Controller input could not be confirmed released."),
    FailureCode.INTERRUPTED_ACTION: _policy("pause/stop arrives during action", "Action interrupted safely.", BotState.PAUSED),
    FailureCode.WORKER_EXCEPTION: _policy("worker catches an unhandled exception", "The bot worker failed. See the log for details."),
    FailureCode.WORKER_STOPPED: _policy("worker exits without requested stop", "The bot worker stopped unexpectedly."),
    FailureCode.RECOVERY_EXHAUSTED: _policy(
        "automatic recovery exceeds its configured total window",
        "Automatic recovery was abandoned after exhausting its recovery window."),
    FailureCode.LATE_SIGNAL: _policy("receiver is destroyed before queued delivery", "A stale UI update was discarded.", BotState.PAUSED),
    FailureCode.DUPLICATE_START: _policy("start requested outside stopped/paused state", "The bot is already starting or running.", BotState.PAUSED),
    FailureCode.STOP_BEFORE_START: _policy("stop requested with no worker", "The bot is already stopped.", BotState.PAUSED),
    FailureCode.WINDOW_CLOSE: _policy("window closes while worker is active", "Stopping the bot before closing."),
    FailureCode.FORCE_CLOSE: _policy("application shutdown hook executes", "Emergency cleanup was requested."),
    FailureCode.MALFORMED_SNAPSHOT: _policy("snapshot schema validation fails", "A malformed worker update was rejected."),
    FailureCode.LOG_OVERFLOW: _policy("log queue exceeds configured bound", "Activity log output was rate-limited.", BotState.PAUSED),
    FailureCode.CONFIG_MISSING: _policy("settings file does not exist", "Default UI settings were loaded.", BotState.PAUSED, "Create defaults once"),
    FailureCode.CONFIG_CORRUPT: _policy("settings JSON cannot be parsed", "Settings were corrupt; the last valid backup was loaded.", BotState.PAUSED, "Load one backup, otherwise defaults"),
    FailureCode.CONFIG_READ_ONLY: _policy("atomic replacement is denied", "Settings could not be saved because the file is read-only.", BotState.PAUSED),
    FailureCode.CONFIG_INVALID: _policy("settings fail schema/range validation", "Invalid settings were rejected.", BotState.PAUSED),
})


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError(f"{label} must be a mapping")
    return value


def _number(value: Any, label: str, *, coordinate: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"{label} must be a number") from exc
    if not math.isfinite(number):
        raise SnapshotError(f"{label} must be finite")
    if coordinate and abs(number) > MAX_WORLD_COORDINATE:
        raise SnapshotError(f"{label} coordinate is outside the supported world")
    return number


def _point(value: Any, label: str) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return (_number(value.get("x"), f"{label}.x", coordinate=True),
                _number(value.get("z"), f"{label}.z", coordinate=True))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (_number(value[0], f"{label}.x", coordinate=True),
                _number(value[1], f"{label}.z", coordinate=True))
    raise SnapshotError(f"{label} point must contain x and z")


@dataclass(frozen=True)
class EntitySnapshot:
    entity_id: str
    kind: str
    x: float
    z: float
    name: str = ""
    valid: bool = False
    ignored: bool = False
    current: bool = False
    distance: float | None = None
    stable_id: bool = True
    alive: bool = True
    despawned: bool = False
    valid_pointer: bool = True
    inside_zone: bool | None = None

    @property
    def valid_monster(self) -> bool:
        return bool(
            self.kind == "monster"
            and self.stable_id
            and self.alive
            and not self.despawned
            and self.valid_pointer
            and self.valid
            and not self.ignored
            and self.inside_zone is True
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EntitySnapshot":
        raw = _mapping(raw, "entity")
        distance = raw.get("distance")
        return cls(
            entity_id=str(raw.get("id", "unknown")),
            kind=str(raw.get("kind", "unknown")),
            x=_number(raw.get("x"), "entity.x", coordinate=True),
            z=_number(raw.get("z"), "entity.z", coordinate=True),
            name=str(raw.get("name", "")),
            valid=bool(raw.get("valid", False)),
            ignored=bool(raw.get("ignored", False)),
            current=bool(raw.get("current", False)),
            distance=None if distance is None else _number(distance, "entity.distance"),
            stable_id=bool(raw.get("stable_id", raw.get("id") not in (None, "", "unknown"))),
            alive=bool(raw.get("alive", raw.get("valid", False))),
            despawned=bool(raw.get("despawned", False)),
            valid_pointer=bool(raw.get("valid_pointer", True)),
        )


@dataclass(frozen=True)
class ZoneSnapshot:
    name: str = ""
    kind: str = "none"
    points: tuple[tuple[float, float], ...] = ()
    circles: tuple[tuple[float, float, float], ...] = ()
    safety_margin: float = 0.0
    auto_return: bool = True
    cell_size: float = 3.0

    @property
    def valid(self) -> bool:
        if not self.name:
            return False
        if self.kind == "polygon":
            if len(self.points) < 3:
                return False
            area = sum(
                x1 * z2 - x2 * z1
                for (x1, z1), (x2, z2) in zip(
                    self.points, self.points[1:] + self.points[:1])
            )
            return abs(area) > 1e-9
        if self.kind == "circles":
            return bool(self.circles) and all(radius > 0.0
                                              for _, _, radius in self.circles)
        if self.kind == "cells":
            return bool(self.points) and self.cell_size > 0.0
        return False

    def is_inside_zone(self, x: float, z: float) -> bool:
        """Exact UI admission shared by counters and marker classification."""
        if not self.valid:
            return False
        if self.kind == "circles":
            return any((x - cx) ** 2 + (z - cz) ** 2 <= radius ** 2 + 1e-9
                       for cx, cz, radius in self.circles)
        if self.kind == "cells":
            half = self.cell_size / 2.0
            return any(abs(x - cx) <= half + 1e-9 and
                       abs(z - cz) <= half + 1e-9
                       for cx, cz in self.points)

        inside = False
        previous = self.points[-1]
        for current in self.points:
            x1, z1 = previous
            x2, z2 = current
            cross = (x - x1) * (z2 - z1) - (z - z1) * (x2 - x1)
            if (abs(cross) <= 1e-9 and
                    min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 and
                    min(z1, z2) - 1e-9 <= z <= max(z1, z2) + 1e-9):
                return True
            if ((z1 > z) != (z2 > z)):
                crossing_x = (x2 - x1) * (z - z1) / (z2 - z1) + x1
                if x < crossing_x:
                    inside = not inside
            previous = current
        return inside

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "points": [list(point) for point in self.points],
            "circles": [list(circle) for circle in self.circles],
            "safety_margin": self.safety_margin,
            "auto_return": self.auto_return,
            "cell_size": self.cell_size,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ZoneSnapshot":
        if raw is None:
            return cls()
        raw = _mapping(raw, "zone")
        points = tuple(_point(point, "zone") for point in raw.get("points", ()))
        circles = []
        for item in raw.get("circles", ()):
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                raise SnapshotError("zone circle must contain x, z, and radius")
            circles.append((_number(item[0], "circle.x", coordinate=True),
                            _number(item[1], "circle.z", coordinate=True),
                            _number(item[2], "circle.radius")))
        margin = _number(raw.get("safety_margin", 0.0), "zone safety margin")
        if margin < 0.0:
            raise SnapshotError("zone safety margin cannot be negative")
        cell_size = _number(raw.get("cell_size", 3.0), "zone cell size")
        return cls(str(raw.get("name", "")), str(raw.get("kind", "none")),
                   points, tuple(circles), margin,
                   bool(raw.get("auto_return", True)), cell_size)


@dataclass(frozen=True)
class BotSnapshot:
    sequence: int = 0
    timestamp: float = 0.0
    state: BotState = BotState.DISCONNECTED
    connected: bool = False
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    automation_state: AutomationState = AutomationState.IDLE
    memory_active: bool = False
    memory_ready: bool = False
    pixel_ready: bool = False
    pixel_error: str = ""
    active_mode: str = "waiting"
    source: str = "pixels"
    bot_state: str = "stopped"
    player: tuple[float, float] | None = None
    player_valid: bool = False
    player_error: str = ""
    target: EntitySnapshot | None = None
    entities: tuple[EntitySnapshot, ...] = ()
    zone: ZoneSnapshot = field(default_factory=ZoneSnapshot)
    path: tuple[tuple[float, float], ...] = ()
    trail: tuple[tuple[float, float], ...] = ()
    logs: tuple[str, ...] = ()
    status: str = ""
    error: str = ""
    scan_version: int = 0
    player_read_version: int = 0
    scan_in_progress: bool = False
    scan_started_at: float = 0.0
    scanner_alive: bool = False
    scan_timed_out: bool = False
    physical_toggle_version: int = 0
    last_scan_completed_at: float = 0.0
    player_read_at: float = 0.0
    player_fresh: bool = False
    process_id: int | None = None
    session_id: str = ""
    memory_session_valid: bool = False
    zone_display_state: ZoneDisplayState = ZoneDisplayState.NO_SAVED_ZONE
    raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}),
                                     compare=False, repr=False)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BotSnapshot":
        raw = _mapping(raw, "snapshot")
        try:
            state = BotState(str(raw.get("state", BotState.DISCONNECTED.value)).upper())
        except ValueError as exc:
            raise SnapshotError("snapshot state is invalid") from exc
        try:
            connection_state = ConnectionState(str(raw.get(
                "connection_state",
                ConnectionState.CONNECTED.value if raw.get("connected", False)
                else ConnectionState.DISCONNECTED.value)).upper())
            automation_state = AutomationState(str(raw.get(
                "automation_state",
                AutomationState.RUNNING.value if state == BotState.RUNNING
                else AutomationState.PAUSED.value if state == BotState.PAUSED
                else AutomationState.IDLE.value)).upper())
        except ValueError as exc:
            raise SnapshotError("snapshot lifecycle state is invalid") from exc
        player_raw = raw.get("player")
        player_error = str(raw.get("player_error", ""))
        player_claimed_valid = bool(raw.get(
            "player_valid", player_raw is not None))
        player = None
        if player_claimed_valid and player_raw is not None:
            player = _point(player_raw, "player")
        player_valid = player_claimed_valid and player is not None
        player_fresh = bool(raw.get("player_fresh", player_valid)) and player_valid
        source = str(raw.get("source", "pixels"))
        active_mode = normalize_mode(raw.get("active_mode", source))
        pixel_ready = bool(raw.get("pixel_ready", False))
        pixel_error = str(raw.get("pixel_error", ""))
        zone = ZoneSnapshot.from_mapping(raw.get("zone"))
        parsed = []
        for item in raw.get("entities", ()):
            entity = EntitySnapshot.from_mapping(item)
            parsed.append(field_replace(
                entity,
                inside_zone=zone.is_inside_zone(entity.x, entity.z)
                            if zone.valid else None,
            ))
        deduplicated = []
        stable_indexes = {}
        for entity in parsed:
            if not entity.stable_id:
                deduplicated.append(entity)
                continue
            previous = stable_indexes.get(entity.entity_id)
            if previous is None:
                stable_indexes[entity.entity_id] = len(deduplicated)
                deduplicated.append(entity)
                continue
            old = deduplicated[previous]
            if (entity.current, entity.valid, entity.valid_pointer) > (
                    old.current, old.valid, old.valid_pointer):
                deduplicated[previous] = entity
        entities = tuple(deduplicated)
        target_raw = raw.get("target")
        target = None
        if target_raw:
            requested = EntitySnapshot.from_mapping(target_raw)
            for entity in entities:
                if entity.stable_id and entity.entity_id == requested.entity_id:
                    target = field_replace(
                        entity, current=True, distance=requested.distance,
                        name=requested.name or entity.name,
                    )
                    break
        path = tuple(_point(point, "path") for point in raw.get("path", ()))
        trail = tuple(_point(point, "trail") for point in raw.get("trail", ()))
        logs = tuple(str(line) for line in raw.get("log", raw.get("logs", ())))[-200:]
        try:
            sequence = int(raw.get("sequence", 0))
            process_raw = raw.get("process_id")
            process_id = None if process_raw in (None, "") else int(process_raw)
        except (TypeError, ValueError) as exc:
            raise SnapshotError("snapshot sequence and process ID must be integers") from exc
        display_raw = raw.get("zone_display_state")
        if display_raw is None:
            if zone.valid:
                display_state = (ZoneDisplayState.ACTIVE if connection_state ==
                                 ConnectionState.CONNECTED else
                                 ZoneDisplayState.LOADED_DISCONNECTED)
            elif zone.name:
                display_state = ZoneDisplayState.INVALID
            else:
                display_state = ZoneDisplayState.NO_SAVED_ZONE
        else:
            try:
                display_state = ZoneDisplayState(str(display_raw).upper())
            except ValueError as exc:
                raise SnapshotError("saved-zone display state is invalid") from exc
        session_id = str(raw.get("session_id", ""))
        has_session_metadata = process_raw not in (None, "") or bool(session_id)
        memory_session_valid = bool(raw.get(
            "memory_session_valid",
            (process_id is not None and process_id > 0 and bool(session_id))
            if has_session_metadata else True,
        ))
        if has_session_metadata:
            memory_session_valid = bool(
                memory_session_valid and process_id is not None
                and process_id > 0 and session_id)
        lifecycle_declared = bool(
            "connection_state" in raw or "connected" in raw
            or has_session_metadata)
        connected = bool(raw.get("connected", False))
        if lifecycle_declared:
            connected = bool(
                connection_state == ConnectionState.CONNECTED
                and memory_session_valid)
            if connection_state == ConnectionState.CONNECTED and not connected:
                connection_state = ConnectionState.DISCONNECTED
            if connection_state != ConnectionState.CONNECTED:
                automation_state = AutomationState.IDLE
                if connection_state == ConnectionState.DISCONNECTED:
                    state = BotState.DISCONNECTED
                player_valid = player_fresh = False
        if connected and (not player_valid or not player_fresh):
            pixel_driving = bool(
                automation_state == AutomationState.RUNNING
                and active_mode == "pixel" and pixel_ready)
            if (automation_state == AutomationState.RUNNING
                    and not pixel_driving):
                automation_state = AutomationState.PAUSED
            if (state in (BotState.RUNNING, BotState.STARTING)
                    and not pixel_driving):
                state = BotState.PAUSED
            player_valid = False
        if not player_valid:
            player = None
            target = None
            path = ()
            trail = ()
            entities = tuple(field_replace(
                entity, current=False, distance=None)
                for entity in entities if entity.kind != "player")
        memory_active = bool(
            raw.get("memory_active", False) and connected
            and memory_session_valid and player_valid and player_fresh)
        memory_ready = bool(raw.get("memory_ready", memory_active)
                            and memory_active)
        if automation_state != AutomationState.RUNNING:
            active_mode = "waiting"
        return cls(
            sequence=sequence,
            timestamp=_number(raw.get("timestamp", 0.0), "snapshot timestamp"),
            state=state,
            connected=connected,
            connection_state=connection_state,
            automation_state=automation_state,
            memory_active=memory_active,
            memory_ready=memory_ready,
            pixel_ready=bool(pixel_ready and connected),
            pixel_error=pixel_error,
            active_mode=active_mode,
            source=source,
            bot_state=str(raw.get("bot_state", "stopped")),
            player=player,
            player_valid=player_valid,
            player_error=player_error,
            target=target,
            entities=entities,
            zone=zone,
            path=path,
            trail=trail,
            logs=logs,
            status=str(raw.get("status", "")),
            error=str(raw.get("error", "")),
            scan_version=int(raw.get("scan_version", sequence)),
            player_read_version=int(raw.get("player_read_version", sequence)),
            scan_in_progress=bool(raw.get("scan_in_progress", False)),
            scan_started_at=_number(raw.get("scan_started_at", 0.0),
                                    "scan start timestamp"),
            scanner_alive=bool(raw.get("scanner_alive", False)),
            scan_timed_out=bool(raw.get("scan_timed_out", False)),
            physical_toggle_version=int(raw.get("physical_toggle_version", 0)),
            last_scan_completed_at=_number(
                raw.get("last_scan_completed_at", 0.0),
                "last scan completion timestamp"),
            player_read_at=_number(raw.get("player_read_at", 0.0),
                                   "player read timestamp"),
            player_fresh=player_fresh,
            process_id=process_id,
            session_id=session_id,
            memory_session_valid=memory_session_valid,
            zone_display_state=display_state,
            raw=MappingProxyType(dict(raw)),
        )

    @classmethod
    def safe(cls, state: BotState = BotState.STOPPED,
             status: str = "Ready") -> "BotSnapshot":
        return cls(state=state, status=status)

    @property
    def monsters_in_zone(self) -> tuple[EntitySnapshot, ...]:
        if not self.zone.valid:
            return ()
        return tuple(entity for entity in self.entities if entity.valid_monster)

    def with_zone(self, zone: ZoneSnapshot,
                  display_state: ZoneDisplayState) -> "BotSnapshot":
        raw = dict(self.raw)
        raw.update({
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "state": self.state.value,
            "connected": self.connected,
            "connection_state": self.connection_state.value,
            "automation_state": self.automation_state.value,
            "memory_active": self.memory_active,
            "memory_ready": self.memory_ready,
            "pixel_ready": self.pixel_ready,
            "pixel_error": self.pixel_error,
            "active_mode": self.active_mode,
            "source": self.source,
            "bot_state": self.bot_state,
            "player": self.player,
            "player_valid": self.player_valid,
            "player_error": self.player_error,
            "player_fresh": self.player_fresh,
            "process_id": self.process_id,
            "session_id": self.session_id,
            "memory_session_valid": self.memory_session_valid,
            "scan_version": self.scan_version,
            "player_read_version": self.player_read_version,
            "scan_in_progress": self.scan_in_progress,
            "scan_started_at": self.scan_started_at,
            "scanner_alive": self.scanner_alive,
            "scan_timed_out": self.scan_timed_out,
            "last_scan_completed_at": self.last_scan_completed_at,
            "player_read_at": self.player_read_at,
            "zone": zone.to_mapping(),
            "zone_display_state": display_state.value,
            "entities": [{
                "id": item.entity_id, "kind": item.kind,
                "x": item.x, "z": item.z, "name": item.name,
                "valid": item.valid, "ignored": item.ignored,
                "current": item.current, "distance": item.distance,
                "stable_id": item.stable_id, "alive": item.alive,
                "despawned": item.despawned,
                "valid_pointer": item.valid_pointer,
            } for item in self.entities],
            "path": [list(point) for point in self.path],
            "trail": [list(point) for point in self.trail],
            "status": self.status,
            "error": self.error,
        })
        if self.target is not None:
            raw["target"] = {
                "id": self.target.entity_id, "kind": self.target.kind,
                "x": self.target.x, "z": self.target.z,
                "name": self.target.name, "valid": self.target.valid,
                "current": True, "distance": self.target.distance,
                "stable_id": self.target.stable_id,
                "alive": self.target.alive,
                "despawned": self.target.despawned,
                "valid_pointer": self.target.valid_pointer,
            }
        return type(self).from_mapping(raw)
