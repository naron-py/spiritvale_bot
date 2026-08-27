"""Shared operating-mode readiness for UI controls and child hotkeys."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StartReadiness:
    game_connected: bool
    memory_ready: bool
    pixel_ready: bool
    can_start: bool
    selected_mode: str
    reason: str


def normalize_mode(value) -> str:
    mode = str(value or "").strip().lower()
    if mode in ("pixel", "pixels", "minimap"):
        return "pixel"
    if mode == "memory":
        return "memory"
    return "waiting"


def evaluate_start_readiness(game_connected: bool, memory_ready: bool,
                             pixel_ready: bool, preferred_mode="memory",
                             memory_reason="memory session or player position unavailable",
                             pixel_reason="capture region or configuration unavailable"):
    connected = bool(game_connected)
    memory = bool(memory_ready)
    pixel = bool(pixel_ready)
    preferred = normalize_mode(preferred_mode)
    if not connected:
        return StartReadiness(False, memory, pixel, False, "waiting",
                              "Game is not connected.")
    available = {"memory": memory, "pixel": pixel}
    if preferred not in available:
        preferred = "memory"
    if available[preferred]:
        selected = preferred
    else:
        fallback = "pixel" if preferred == "memory" else "memory"
        selected = fallback if available[fallback] else "waiting"
    if selected != "waiting":
        return StartReadiness(True, memory, pixel, True, selected,
                              f"{selected.title()} Mode ready")
    reason = (f"Memory unavailable: {memory_reason or 'unknown reason'}; "
              f"Pixel unavailable: {pixel_reason or 'unknown reason'}")
    return StartReadiness(True, memory, pixel, False, "waiting", reason)
