"""Child-process bridge around the terminal bot.

Narrow state-request and dashboard hooks provide command-queue and JSON-snapshot
adapters. Target arbitration, safety gates, and pad cleanup remain the terminal
implementations.
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import math
import struct
import sys
import threading
import time
from typing import Any, Callable

from .model import BotSnapshot
from .process_discovery import find_spiritvale_pids
from .readiness import evaluate_start_readiness, normalize_mode


SNAPSHOT_PREFIX = "@@UI_SNAPSHOT "
EVENT_PREFIX = "@@UI_EVENT "
PLAYER_READ_TTL_S = 2.0
SCAN_DELAY_TIMEOUT_S = 30.0


class StopRequested(KeyboardInterrupt):
    """Raised only at the terminal loop's normal toggle boundary.

    KeyboardInterrupt is intentional: minimap_bot.main catches it and always
    reaches its existing finally block that closes the controller backend.
    """


class CommandGate:
    ALLOWED = frozenset(("resume", "pause", "toggle", "stop", "emergency",
                         "configure", "memory_wait", "memory_recovered"))

    def __init__(self, event: Callable[[str], None] | None = None,
                 controller_config=None):
        self._lock = threading.Lock()
        self._desired = None
        self._observed = False
        self._stop = None
        self._memory_wait = False
        self._internal_pending = None
        self._physical_toggle_version = 0
        self._can_start = False
        self._start_mode = "waiting"
        self._start_reason = "Waiting for targeting readiness."
        self._monitor_at = time.monotonic()
        self._last_player_read = 0.0
        self._last_entity_scan = 0.0
        self._scan_in_progress = False
        self._scan_started_at = 0.0
        self._controller_config = dict(controller_config or {})
        self._controller_config_pending = bool(controller_config)
        self.event = event or (lambda _message: None)

    def submit(self, command: str, config=None) -> bool:
        command = str(command).strip().lower()
        if command not in self.ALLOWED:
            self.event(f"ignored invalid command {command!r}")
            return False
        with self._lock:
            if command in ("stop", "emergency"):
                self._stop = command
            elif command == "configure":
                if not isinstance(config, dict):
                    self.event("ignored invalid controller configuration")
                    return False
                self._controller_config = json.loads(json.dumps(config))
                self._controller_config_pending = True
            elif command == "memory_wait":
                if not self._memory_wait:
                    self._memory_wait = True
                    self._internal_pending = "wait"
            elif command == "memory_recovered":
                if self._memory_wait:
                    self._memory_wait = False
                    if self._desired:
                        self._internal_pending = "running"
            elif command == "resume":
                self._memory_wait = False
                self._internal_pending = None
                self._desired = True
            elif command == "pause":
                self._memory_wait = False
                self._internal_pending = None
                self._desired = False
            else:
                base = self._observed if self._desired is None else self._desired
                self._desired = not base
        return True

    def poll_controller_config(self):
        with self._lock:
            if not self._controller_config_pending:
                return None
            self._controller_config_pending = False
            return json.loads(json.dumps(self._controller_config))

    def observe(self, running: bool):
        with self._lock:
            self._observed = bool(running)
            if self._desired is None:
                self._desired = self._observed

    def poll_toggle(self) -> bool:
        with self._lock:
            self._monitor_at = time.monotonic()
            if self._stop is not None:
                raise StopRequested(self._stop)
            if self._memory_wait:
                return False
            if self._desired is None or self._desired == self._observed:
                return False
            # Optimistic reconciliation prevents another poll from issuing the
            # same edge before the next dashboard snapshot confirms the state.
            self._observed = self._desired
            return True

    def poll_internal(self):
        with self._lock:
            action = self._internal_pending
            self._internal_pending = None
            if action == "wait":
                self._observed = False
            elif action == "running":
                self._observed = True
            return action

    def set_start_readiness(self, can_start, mode, reason):
        with self._lock:
            self._can_start = bool(can_start)
            self._start_mode = normalize_mode(mode)
            self._start_reason = str(reason)

    def update_health(self, world):
        with self._lock:
            self._monitor_at = time.monotonic()
            self._last_player_read = float(world.get("player_read_at", 0.0) or 0.0)
            self._last_entity_scan = float(
                world.get("last_scan_completed_at", 0.0) or 0.0)
            self._scan_in_progress = bool(world.get("scan_in_progress", False))
            self._scan_started_at = float(world.get("scan_started_at", 0.0) or 0.0)

    def heartbeat(self):
        with self._lock:
            return {
                "type": "heartbeat", "at": time.monotonic(),
                "monitor_loop_alive": time.monotonic() - self._monitor_at < 3.0,
                "last_player_read": self._last_player_read,
                "last_entity_scan": self._last_entity_scan,
                "scan_in_progress": self._scan_in_progress,
                "scan_started_at": self._scan_started_at,
            }

    def allow_hotkey_toggle(self) -> bool:
        """Use the same readiness gate as UI START; stopping is always allowed."""
        with self._lock:
            if self._observed:
                self._desired = False
                self._physical_toggle_version += 1
                return True
            allowed = self._can_start
            mode = self._start_mode
            reason = ("memory recovery is waiting for a fresh scan"
                      if self._memory_wait else self._start_reason)
            if allowed:
                self._memory_wait = False
                self._internal_pending = None
                self._desired = True
                self._physical_toggle_version += 1
        if allowed:
            self.event(f"End hotkey starting {mode.upper()} mode")
            return True
        self.event(f"End hotkey start blocked: {reason}")
        return False

    @property
    def physical_toggle_version(self) -> int:
        with self._lock:
            return self._physical_toggle_version


def _safe_point(value):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    # Memory positions are (x,y,z); route points are generally (x,z).
    x = value[0]
    z = value[2] if len(value) >= 3 else value[1]
    try:
        x, z = float(x), float(z)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or not math.isfinite(z) or max(abs(x), abs(z)) > 1e7:
        return None
    return [x, z]


def pixel_capture_readiness(bot_module):
    """Validate the static capture contract without grabbing the desktop."""
    region = getattr(bot_module, "MINIMAP", None)
    if not isinstance(region, dict):
        return False, "MINIMAP capture region is missing"
    try:
        cx = float(region.get("cx"))
        cy = float(region.get("cy"))
        radius = float(region.get("r"))
    except (TypeError, ValueError):
        return False, "MINIMAP capture region must contain numeric cx, cy, and r"
    values = (cx, cy, radius)
    if not all(math.isfinite(value) for value in values):
        return False, "MINIMAP capture region contains a non-finite value"
    if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0 and radius > 0.0):
        return False, "MINIMAP capture region is outside normalized client bounds"
    for name in ("minimap_region", "find_red_dots"):
        if not callable(getattr(bot_module, name, None)):
            return False, f"pixel capture helper {name} is unavailable"
    return True, ""


def _read_player(eyes, owner, units):
    """Read the resolved owner with terminal semantics, then a scan-row fallback."""
    if owner is None:
        return None, None, False, "local player owner unavailable"
    raw = None
    error = ""
    reader = getattr(eyes, "_positions", None)
    if callable(reader):
        try:
            raw = reader([owner]).get(owner)
        except (OSError, TypeError, ValueError, struct.error) as exc:
            error = str(exc)
    if raw is None and not callable(reader):
        owner_row = next((row for row in units
                          if isinstance(row, (list, tuple)) and len(row) >= 5
                          and row[1] == owner), None)
        if owner_row is not None:
            raw = (owner_row[2], owner_row[3], owner_row[4])
    parsed = _safe_point(raw)
    if parsed is not None and abs(parsed[0]) <= 1e-9 and abs(parsed[1]) <= 1e-9:
        parsed = None
        error = error or "zeroed owner position"
    if parsed is None:
        error = error or "owner position unavailable"
    return raw, parsed, parsed is not None, error


def _world_copy(eyes, now: float, max_entities: int = 250):
    if eyes is None:
        return None, [], None, {"name": "", "kind": "none"}, []
    with eyes.lock:
        units = list(getattr(eyes, "units", ()))
        seen = dict(getattr(eyes, "seen_at", {}))
        report = dict(getattr(eyes, "scan_summary", {}))
        fight_ok = dict(getattr(eyes, "fight_ok", {}))
        ignored = dict(getattr(eyes, "ignored", {}))
        me = getattr(eyes, "me", None) or getattr(eyes, "owner", None)
        chasing = getattr(eyes, "chasing", None)
        chasing_id = getattr(eyes, "chasing_id", None)
        target_name = getattr(eyes, "target_name", "")
        area = getattr(eyes, "area", None)
        path = list(getattr(eyes, "path", ()) or ())
        last_pos = getattr(eyes, "last_pos", None)

    names = report.get("monster_names", {}) if isinstance(report, dict) else {}
    player = _safe_point(last_pos) or _safe_point(seen.get(me))
    entities = []
    for kind, address, *position in units:
        point = _safe_point(seen.get(address)) or _safe_point(position)
        if point is None:
            continue
        cached = fight_ok.get(address, ())
        attackable = bool(len(cached) >= 2 and cached[0] >= now and cached[1])
        is_ignored = ignored.get(address, 0.0) >= now
        entity_id = address
        if address == chasing and chasing_id is not None:
            entity_id = chasing_id
        entities.append({
            "id": str(entity_id), "address": f"0x{address:X}",
            "kind": str(kind), "x": point[0], "z": point[1],
            "name": str(names.get(address, target_name if address == chasing else "")),
            "valid": bool(kind == "monster" and attackable and not is_ignored),
            "ignored": is_ignored, "current": address == chasing,
        })
        if len(entities) >= max_entities:
            break

    target = None
    if chasing is not None:
        location = _safe_point(seen.get(chasing))
        if location is not None:
            distance = None
            if player is not None:
                distance = math.hypot(location[0] - player[0], location[1] - player[1])
            target = {"id": str(chasing_id if chasing_id is not None else chasing),
                      "kind": "monster", "x": location[0], "z": location[1],
                      "name": str(target_name or names.get(chasing, "unknown")),
                      "valid": True, "current": True, "distance": distance}

    zone = {"name": "", "kind": "none", "points": [], "circles": [],
            "safety_margin": 0.0, "auto_return": True}
    if area is not None:
        zone["name"] = str(getattr(area, "name", ""))
        polygon = getattr(area, "polygon", ())
        circles = getattr(area, "circles", ())
        cells = getattr(area, "cells", ())
        if polygon:
            zone.update(kind="polygon", points=[[float(x), float(z)]
                                                 for x, z in polygon])
        elif circles:
            zone.update(kind="circles", circles=[[float(x), float(z), float(r)]
                                                  for x, z, r in circles])
        elif cells:
            cell = float(getattr(area, "cell", 3.0))
            zone.update(kind="cells", points=[[(x + 0.5) * cell, (z + 0.5) * cell]
                                               for x, z in list(cells)[:2000]])
        try:
            import minimap_bot
            zone["safety_margin"] = float(minimap_bot.AREA_SAFETY)
        except Exception:
            pass

    route = [point for item in path if (point := _safe_point(item)) is not None]
    return player, entities, target, zone, route


def _zone_mapping(area):
    zone = {"name": "", "kind": "none", "points": [], "circles": [],
            "safety_margin": 0.0, "auto_return": True, "cell_size": 3.0}
    if area is None:
        return zone
    zone["name"] = str(getattr(area, "name", ""))
    polygon = getattr(area, "polygon", ())
    circles = getattr(area, "circles", ())
    cells = getattr(area, "cells", ())
    if polygon:
        zone.update(kind="polygon", points=[[float(x), float(z)]
                                             for x, z in polygon])
    elif circles:
        zone.update(kind="circles", circles=[[float(x), float(z), float(r)]
                                              for x, z, r in circles])
    elif cells:
        cell = float(getattr(area, "cell", 3.0))
        zone.update(kind="cells", cell_size=cell,
                    points=[[(x + 0.5) * cell, (z + 0.5) * cell]
                            for x, z in list(cells)[:2000]])
    return zone


def _entity_mapping(entity):
    return {
        "id": entity.entity_id,
        "kind": entity.kind,
        "x": entity.x,
        "z": entity.z,
        "name": entity.name,
        "valid": entity.valid,
        "ignored": entity.ignored,
        "current": entity.current,
        "distance": entity.distance,
        "stable_id": entity.stable_id,
        "alive": entity.alive,
        "despawned": entity.despawned,
        "valid_pointer": entity.valid_pointer,
    }


class ScanEntityCache:
    """One canonical immutable UI entity set for each terminal scan pass."""

    def __init__(self, max_entities=250):
        self.max_entities = max(0, int(max_entities))
        self._key = None
        self._world = None
        self._scan_at = 0.0
        self._had_player = False
        self._player_read_version = 0

    def capture(self, eyes, now=None):
        now = time.time() if now is None else float(now)
        if eyes is None:
            world = {"version": 0, "captured_at": now, "player": None,
                     "player_raw": None, "player_valid": False,
                     "player_error": "memory reader unavailable",
                     "entities": (), "target": None,
                     "zone": _zone_mapping(None), "route": (),
                     "total": 0, "hostile": 0, "unique": 0,
                     "inside_zone": 0, "valid_targets": 0,
                     "connection_state": "DISCONNECTED", "error": ""}
            return world, self._key is not None
        coherent = False
        for _attempt in range(2):
            with eyes.lock:
                generation = int(getattr(eyes, "generation", 0))
                version = int(getattr(
                    eyes, "scan_version", getattr(eyes, "scan_passes", 0)))
                units = list(getattr(eyes, "units", ()))
                report = dict(getattr(eyes, "scan_summary", {}))
                ignored = dict(getattr(eyes, "ignored", {}))
                ignored_ids = dict(getattr(eyes, "ignored_ids", {}))
                owner = getattr(eyes, "me", None) or getattr(eyes, "owner", None)
                chasing = getattr(eyes, "chasing", None)
                chasing_id = getattr(eyes, "chasing_id", None)
                target_name = getattr(eyes, "target_name", "")
                area = getattr(eyes, "area", None)
                route_source = list(getattr(eyes, "path", ()) or ())
                scan_error = str(getattr(eyes, "scan_error", ""))
                fight_ok = dict(getattr(eyes, "fight_ok", {}))
                scan_in_progress = bool(
                    getattr(eyes, "scan_in_progress", False))
                scan_started_at = float(
                    getattr(eyes, "scan_started_at", 0.0) or 0.0)
                last_scan_completed_at = float(
                    getattr(eyes, "last_scan_completed_at", 0.0) or 0.0)
                scanner = getattr(eyes, "scanner", None)
                scanner_alive = bool(
                    scanner is not None and scanner.is_alive())
            player_raw, player, player_valid, player_error = _read_player(
                eyes, owner, units)
            self._player_read_version += 1
            with eyes.lock:
                current_version = int(getattr(
                    eyes, "scan_version", getattr(eyes, "scan_passes", 0)))
                current_owner = (getattr(eyes, "me", None)
                                 or getattr(eyes, "owner", None))
                coherent = bool(
                    generation == int(getattr(eyes, "generation", 0))
                    and version == current_version
                    and owner == current_owner)
            if coherent:
                break
        if not coherent:
            player_raw = player = None
            player_valid = False
            player_error = "memory generation changed during player read"
        key = (id(eyes), version, owner, chasing, chasing_id)
        if key == self._key and self._world is not None:
            world = dict(self._world)
            target = None if world["target"] is None else dict(world["target"])
            if target is not None:
                target["distance"] = (None if player is None else math.hypot(
                    float(target["x"]) - player[0],
                    float(target["z"]) - player[1]))
            world.update(
                player=player, player_raw=player_raw,
                player_valid=player_valid, player_error=player_error,
                player_read_at=now,
                player_read_version=self._player_read_version,
                scan_in_progress=scan_in_progress,
                scan_started_at=scan_started_at,
                scanner_alive=scanner_alive,
                last_scan_completed_at=last_scan_completed_at,
                target=target,
            )
            self._world = world
            return world, False
        if self._key is not None and self._key[0] != id(eyes):
            self._had_player = False
        if (self._key is None or self._key[0] != id(eyes)
                or self._key[1] != version):
            self._scan_at = now

        names = report.get("monster_names", {}) if isinstance(report, dict) else {}
        rows = list(units)
        if self.max_entities and len(rows) > self.max_entities:
            selected = rows[:self.max_entities]
            if chasing is not None and not any(
                    len(row) > 1 and row[1] == chasing for row in selected):
                current_row = next((row for row in rows
                                    if len(row) > 1 and row[1] == chasing), None)
                if current_row is not None:
                    selected[-1] = current_row
            rows = selected
        raw_entities = []
        hostile = sum(1 for row in units
                      if isinstance(row, (list, tuple)) and row
                      and row[0] == "monster")
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            kind, address, x, _y, z = row[:5]
            point = _safe_point((x, z))
            pointer_ok = (isinstance(address, int)
                          and 0x10000 < address < 0x7FFFFFFFFFFF)
            if point is None or not pointer_ok:
                continue
            stable_id = None
            try:
                stable_id = eyes._stable_id(address)
            except (AttributeError, OSError, TypeError, ValueError, struct.error):
                if address == chasing:
                    stable_id = chasing_id
            alive = True
            invisible = False
            if kind == "monster":
                try:
                    alive, invisible = eyes.ms.monster_target_state(
                        eyes.mem, address)
                except (AttributeError, OSError, TypeError, ValueError,
                        struct.error):
                    cached = fight_ok.get(address, ())
                    alive = bool(len(cached) >= 2 and cached[0] >= now
                                 and cached[1])
                    invisible = bool(len(cached) >= 3 and cached[2])
            is_ignored = (ignored.get(address, 0.0) >= now or
                          (stable_id is not None and
                           ignored_ids.get(stable_id, 0.0) >= now))
            stable = stable_id is not None
            raw_entities.append({
                "id": str(stable_id) if stable else f"ptr:{address:X}",
                "kind": str(kind), "x": point[0], "z": point[1],
                "name": str(names.get(
                    address, target_name if address == chasing else "")),
                "valid": bool(kind == "monster" and stable and alive
                              and not invisible and not is_ignored),
                "ignored": is_ignored,
                "current": address == chasing,
                "stable_id": stable,
                "alive": bool(alive),
                "despawned": bool(kind == "monster" and not alive),
                "valid_pointer": pointer_ok,
            })


        zone = _zone_mapping(area)
        canonical = BotSnapshot.from_mapping({
            "sequence": version, "timestamp": now, "state": "PAUSED",
            "connected": True, "memory_active": version > 0,
            "player": None if player is None else {"x": player[0], "z": player[1]},
            "entities": raw_entities, "zone": zone,
        })
        entities = tuple(_entity_mapping(entity)
                         for entity in canonical.entities)
        current = next((entity for entity in canonical.entities
                        if entity.current), None)
        target = None
        if current is not None:
            target = _entity_mapping(current)
            if player is not None:
                target["distance"] = math.hypot(
                    current.x - player[0], current.z - player[1])
        unique = len({entity.entity_id for entity in canonical.entities
                      if entity.kind == "monster" and entity.stable_id})
        valid_targets = sum(
            entity.kind == "monster" and entity.valid and entity.stable_id
            and entity.alive and not entity.despawned and entity.valid_pointer
            and not entity.ignored
            for entity in canonical.entities)
        if scan_error:
            connection_state = "ERROR"
        elif player is not None:
            connection_state = "CONNECTED"
            self._had_player = True
        elif self._had_player:
            connection_state = "DISCONNECTED"
        else:
            connection_state = "CONNECTING"
        world = {
            "version": version, "captured_at": self._scan_at, "player": player,
            "player_raw": player_raw, "player_valid": player_valid,
            "player_error": player_error,
            "player_read_at": now,
            "player_read_version": self._player_read_version,
            "scan_in_progress": scan_in_progress,
            "scan_started_at": scan_started_at,
            "scanner_alive": scanner_alive,
            "last_scan_completed_at": last_scan_completed_at,
            "entities": entities, "target": target, "zone": zone,
            "route": tuple(point for item in route_source
                           if (point := _safe_point(item)) is not None),
            "total": len(units), "hostile": hostile, "unique": unique,
            "inside_zone": len(canonical.monsters_in_zone),
            "valid_targets": valid_targets,
            "connection_state": connection_state, "error": scan_error,
        }
        new_scan = self._key is None or self._key[1] != version
        self._key, self._world = key, world
        return world, new_scan


def build_snapshot(info: dict[str, Any], eyes, sequence: int,
                   trail: list[tuple[float, float]] | deque,
                   max_entities: int = 250, scan_cache=None,
                   scan_world=None, scan_is_new=None, process_id=None,
                   session_id="", preferred_mode="memory",
                   pixel_ready=False, pixel_error="", now=None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    if scan_world is None:
        cache = scan_cache or ScanEntityCache(max_entities)
        scan_world, captured_new = cache.capture(eyes, now)
        if scan_is_new is None:
            scan_is_new = captured_new
    player = scan_world["player"]
    entities = list(scan_world["entities"])
    target = scan_world["target"]
    zone = dict(scan_world["zone"])
    route = list(scan_world["route"])
    requested_running = bool(info.get("running", False))
    has_session_metadata = process_id is not None or bool(session_id)
    memory_session_valid = bool(
        int(process_id or 0) > 0 and str(session_id)) if has_session_metadata else True
    fresh_player = bool(scan_world.get("player_valid", player is not None)
                        and player is not None
                        and now - float(scan_world.get("player_read_at", 0.0))
                        <= PLAYER_READ_TTL_S)
    scanner_alive = bool(scan_world.get("scanner_alive", False))
    scan_in_progress = bool(scan_world.get("scan_in_progress", False))
    last_scan_completed_at = float(
        scan_world.get("last_scan_completed_at", 0.0) or 0.0)
    scan_delay_started_at = (float(
        scan_world.get("scan_started_at", 0.0) or 0.0)
        or last_scan_completed_at)
    scan_timed_out = bool(
        (scan_world["version"] > 0 and not scanner_alive)
        or (scan_in_progress and scan_delay_started_at > 0.0
            and now - scan_delay_started_at > SCAN_DELAY_TIMEOUT_S))
    memory_ready = bool(scan_world["version"] > 0 and memory_session_valid
                        and fresh_player and not scan_world["error"]
                        and not scan_timed_out)
    readiness = evaluate_start_readiness(
        memory_session_valid, memory_ready, pixel_ready, preferred_mode,
        str(scan_world.get("player_error") or scan_world.get("error")
            or "memory session or player position unavailable"),
        str(pixel_error or "capture region or configuration unavailable"))
    source_mode = normalize_mode(info.get("source"))
    if source_mode == "waiting":
        source_mode = readiness.selected_mode
    source_ready = (memory_ready if source_mode == "memory" else
                    bool(pixel_ready) if source_mode == "pixel" else False)
    running = bool(requested_running and memory_session_valid and source_ready)
    if not running:
        state = "PAUSED"
    elif info.get("status"):
        state = "PAUSED"
    else:
        state = "RUNNING"
    stick = info.get("stick", (0.0, 0.0)) if running else (0.0, 0.0)
    attack = bool(info.get("attack", False)) if running else False
    connection_state = "CONNECTED" if memory_session_valid else "DISCONNECTED"
    if not fresh_player:
        target = None
        route = []
        trail = []
        entities = [{**entity, "current": False, "distance": None}
                    for entity in entities if entity.get("kind") != "player"]
    player_error = str(scan_world.get("player_error", ""))
    snapshot_error = str(
        scan_world["error"] or player_error
        if source_mode == "memory" and not memory_ready else "")
    debug = (f"[Scan] total={scan_world['total']} "
             f"hostile={scan_world['hostile']} unique={scan_world['unique']} "
             f"inside_zone={scan_world['inside_zone']} "
             f"valid_targets={scan_world['valid_targets']} "
             f"snapshot={scan_world['version']}")
    return {
        "sequence": int(sequence), "timestamp": now, "state": state,
        "connection_state": connection_state,
        "automation_state": ("RUNNING" if running else
                             "PAUSED" if requested_running else "IDLE"),
        "connected": connection_state == "CONNECTED",
        "memory_session_valid": memory_session_valid,
        "memory_active": memory_ready,
        "memory_ready": memory_ready,
        "pixel_ready": bool(pixel_ready),
        "pixel_error": str(pixel_error),
        "active_mode": source_mode if running else "waiting",
        "start_mode": readiness.selected_mode,
        "start_reason": readiness.reason,
        "can_start": readiness.can_start,
        "source": str(info.get("source", "pixels")),
        "bot_state": str(info.get("state", "paused")),
        "player": None if player is None else {"x": player[0], "z": player[1]},
        "player_valid": fresh_player,
        "player_error": player_error,
        "player_fresh": fresh_player,
        "process_id": process_id,
        "session_id": str(session_id),
        "scan_version": int(scan_world["version"]),
        "player_read_version": int(scan_world.get("player_read_version", 0)),
        "player_read_at": float(scan_world.get("player_read_at", 0.0) or 0.0),
        "scan_in_progress": scan_in_progress,
        "scan_started_at": float(scan_world.get("scan_started_at", 0.0) or 0.0),
        "scanner_alive": scanner_alive,
        "scan_timed_out": scan_timed_out,
        "last_scan_completed_at": last_scan_completed_at,
        "target": target, "entities": entities, "zone": zone,
        "path": route, "trail": [[float(x), float(z)] for x, z in trail],
        "log": [debug] if scan_is_new else [],
        "status": str(info.get("status") or info.get("warning") or ""),
        "error": snapshot_error,
        "control": {"stick": [float(stick[0]), float(stick[1])],
                    "attack": attack},
        "dashboard": info,
    }


class JsonDashboard:
    def __init__(self, bot_module, bot_mode=None, max_entities=250,
                 trail_length=120, gate=None, expected_pid=0, session_id="",
                 monitor_eyes=None, preferred_mode="memory",
                 pixel_ready=False, pixel_error=""):
        self.bot = bot_module
        self.bot_mode = bot_mode
        self.max_entities = max_entities
        self.trail = deque(maxlen=trail_length)
        self.sequence = 0
        self.last = 0.0
        self.gate = gate
        self.expected_pid = int(expected_pid)
        self.session_id = str(session_id)
        self.monitor_eyes = monitor_eyes
        self.preferred_mode = str(preferred_mode)
        self.pixel_ready = bool(pixel_ready)
        self.pixel_error = str(pixel_error)
        self.scan_cache = ScanEntityCache(max_entities)
        self._connection_state = "DISCONNECTED"

    def update(self, eyes, running, state, sx=0.0, sy=0.0, attack=False,
               action="", on_loot=False, distance=None, memory_driving=None,
               status=None, force=False):
        now = time.monotonic()
        if not force and now - self.last < 1 / 15:
            return
        self.last = now
        scan_eyes = eyes if eyes is not None else self.monitor_eyes
        if scan_eyes is not None and self.expected_pid:
            attached_pid = int(getattr(getattr(scan_eyes, "mem", None), "pid", 0))
            if attached_pid and attached_pid != self.expected_pid:
                raise RuntimeError(
                    f"memory attached to PID {attached_pid}, expected {self.expected_pid}")
        if (scan_eyes is not None and
                (getattr(scan_eyes, "scanner", None) is None
                 or not scan_eyes.scanner.is_alive())):
            try:
                scan_eyes.start_scanning()
            except Exception as exc:
                print(EVENT_PREFIX + json.dumps({
                    "level": "ERROR",
                    "message": f"read-only scanner could not start: {exc}",
                }), flush=True)
        if self.gate is not None:
            self.gate.observe(running)
        info = self.bot.dashboard_snapshot(
            eyes, running, state, sx, sy, attack, action, on_loot, distance,
            memory_driving, status, self.bot_mode)
        scan_world, scan_is_new = self.scan_cache.capture(scan_eyes, time.time())
        if self.gate is not None:
            self.gate.update_health(scan_world)
        player = scan_world["player"]
        if player is not None and (not self.trail or tuple(player) != self.trail[-1]):
            self.trail.append(tuple(player))
        self.sequence += 1
        raw = build_snapshot(
            info, eyes, self.sequence, self.trail, self.max_entities,
            scan_world=scan_world, scan_is_new=scan_is_new,
            process_id=self.expected_pid, session_id=self.session_id,
            preferred_mode=self.preferred_mode,
            pixel_ready=self.pixel_ready, pixel_error=self.pixel_error)
        if self.gate is not None:
            raw["physical_toggle_version"] = self.gate.physical_toggle_version
            self.gate.set_start_readiness(
                raw["can_start"], raw["start_mode"], raw["start_reason"])
        if scan_is_new:
            raw_value = scan_world.get("player_raw")
            parsed = scan_world.get("player")
            raw["log"].append(
                f"[PlayerRead] pid={self.expected_pid} raw="
                f"{None if raw_value is None else tuple(raw_value)} parsed="
                f"{None if parsed is None else tuple(parsed)} "
                f"valid={raw['player_valid']} error={raw['player_error']}")
            raw["log"].append(
                f"[Snapshot] seq={self.sequence} "
                f"player_valid={raw['player_valid']} entities={len(raw['entities'])}")
        new_connection = raw["connection_state"]
        if new_connection != self._connection_state:
            reason = (raw["error"] or "live PID and memory session")
            raw["log"].append(
                f"[Connection] old={self._connection_state} "
                f"new={new_connection} reason={reason}")
            self._connection_state = new_connection
        if raw.get("zone") is not None:
            raw["zone"]["safety_margin"] = float(
                getattr(self.bot, "AREA_SAFETY", 0.0))
        print(SNAPSHOT_PREFIX + json.dumps(raw, separators=(",", ":")),
              flush=True)


def _stdin_reader(gate: CommandGate):
    for line in sys.stdin:
        try:
            payload = json.loads(line)
            command = payload.get("command", "")
            if command == "ping":
                print(EVENT_PREFIX + json.dumps(gate.heartbeat()), flush=True)
            else:
                gate.submit(command, payload.get("config"))
        except Exception as exc:
            print(EVENT_PREFIX + json.dumps({"level": "ERROR",
                  "message": f"invalid parent command: {exc}"}), flush=True)


def bot_invocation(mode, area="", bot_file="minimap_bot.py"):
    pixel = str(mode) == "minimap"
    active_area = "" if pixel else str(area).strip()
    argv = [str(bot_file), "--minimap" if pixel else "--memory"]
    if active_area:
        argv += ["--area", active_area]
    notice = ("Polygon unavailable in Pixel Mode"
              if pixel and str(area).strip() else "")
    return argv, active_area, notice


def child_main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("memory", "minimap"), default="memory")
    parser.add_argument("--area", default="")
    parser.add_argument("--max-entities", type=int, default=250)
    parser.add_argument("--trail-length", type=int, default=120)
    parser.add_argument("--expected-pid", type=int, default=0)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--no-reconnect", action="store_true")
    parser.add_argument("--control-config", default="{}")
    options = parser.parse_args(argv)

    import minimap_bot as bot

    try:
        initial_control_config = json.loads(options.control_config)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid controller configuration: {exc}")
    gate = CommandGate(lambda message: print(EVENT_PREFIX + json.dumps(
        {"level": "WARNING", "message": message}), flush=True),
        initial_control_config)
    pixel_ready, pixel_error = pixel_capture_readiness(bot)
    monitor_eyes = None
    if options.mode == "minimap" and getattr(bot, "MEMORY_TARGETING", False):
        try:
            # Explicit Pixel actuation stays --minimap. This separate object is
            # read-only and exists only so memory readiness can recover in the
            # background without silently changing the active targeting mode.
            monitor_eyes = bot.MemoryEyes(None)
        except Exception as exc:
            print(EVENT_PREFIX + json.dumps({
                "level": "WARNING",
                "message": f"background memory monitor unavailable: {exc}",
            }), flush=True)
    original_key = bot.toggle_key_hit

    def ui_key():
        if gate.poll_toggle():
            return True
        return bool(original_key() and gate.allow_hotkey_toggle())

    bot.toggle_key_hit = ui_key
    bot.automation_state_request = gate.poll_internal
    bot.controller_config_request = gate.poll_controller_config
    bot.TerminalDashboard = lambda bot_mode=None: JsonDashboard(
        bot, bot_mode, max_entities=max(10, min(2000, options.max_entities)),
        trail_length=max(0, min(5000, options.trail_length)), gate=gate,
        expected_pid=options.expected_pid, session_id=options.session_id,
        monitor_eyes=monitor_eyes, preferred_mode=options.mode,
        pixel_ready=pixel_ready, pixel_error=pixel_error)
    if options.no_reconnect:
        bot.RECONNECT = False
    sys.argv, active_area, area_notice = bot_invocation(
        options.mode, options.area,
        str(getattr(bot, "__file__", "minimap_bot.py")))
    if area_notice:
        print(EVENT_PREFIX + json.dumps({
            "level": "INFO",
            "message": area_notice,
        }), flush=True)
    reader = threading.Thread(target=_stdin_reader, args=(gate,), daemon=True,
                              name="ui-command-reader")
    reader.start()
    print(EVENT_PREFIX + json.dumps({"level": "INFO",
          "message": "runtime child ready"}), flush=True)
    try:
        bot.main(area=active_area or None)
        return 0
    except Exception as exc:
        game_closed = False
        if options.expected_pid:
            try:
                game_closed = options.expected_pid not in find_spiritvale_pids()
            except OSError:
                pass
        if game_closed:
            print(EVENT_PREFIX + json.dumps({"level": "INFO",
                  "message": f"game process PID {options.expected_pid} closed"}),
                  flush=True)
            return 0
        print(EVENT_PREFIX + json.dumps({"level": "ERROR",
              "message": f"runtime child failed: {exc}",
              "traceback": __import__("traceback").format_exc()}), flush=True)
        return 1
    finally:
        print(EVENT_PREFIX + json.dumps({"level": "INFO",
              "message": "runtime child stopped; controller cleanup complete"}),
              flush=True)


if __name__ == "__main__":
    raise SystemExit(child_main())
