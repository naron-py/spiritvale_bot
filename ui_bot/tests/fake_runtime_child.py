"""Protocol-compatible child used only by ProcessRuntime integration tests."""

import argparse
import json
import sys
import time

from ui_bot.runtime_child import EVENT_PREFIX, SNAPSHOT_PREFIX


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--expected-pid", type=int, default=0)
parser.add_argument("--session-id", default="")
parser.add_argument("--mode", choices=("memory", "minimap"), default="memory")
options, _unknown = parser.parse_known_args()


def emit(state, sequence):
    print(SNAPSHOT_PREFIX + json.dumps({
        "sequence": sequence, "timestamp": float(sequence), "state": state,
        "connection_state": "CONNECTED", "automation_state": (
            "RUNNING" if state == "RUNNING" else "IDLE"),
        "connected": True, "memory_active": True, "memory_ready": True,
        "pixel_ready": True, "pixel_error": "",
        "source": "pixels" if options.mode == "minimap" else "memory",
        "active_mode": ("pixel" if options.mode == "minimap" else "memory")
                       if state == "RUNNING" else "waiting",
        "player": {"x": 10.0, "z": 20.0}, "player_fresh": True,
        "player_read_version": sequence, "player_read_at": float(sequence),
        "scan_version": sequence, "last_scan_completed_at": float(sequence),
        "scan_in_progress": False, "scan_started_at": 0.0,
        "process_id": options.expected_pid, "session_id": options.session_id,
        "bot_state": state.lower(),
    }), flush=True)


print(EVENT_PREFIX + json.dumps({"level": "INFO", "message": "fake ready"}),
      flush=True)
emit("PAUSED", 1)
sequence = 1
ignore_emergency = False
ignore_ping = False
for line in sys.stdin:
    request = json.loads(line)
    command = request["command"]
    sequence += 1
    if command == "ignore-emergency":
        ignore_emergency = True
    elif command == "ignore-ping":
        ignore_ping = True
    elif command == "ping":
        if not ignore_ping:
            print(EVENT_PREFIX + json.dumps({
                "type": "heartbeat", "at": time.monotonic(),
                "monitor_loop_alive": True,
            }), flush=True)
    elif command == "crash":
        raise SystemExit(7)
    elif command == "resume":
        emit("RUNNING", sequence)
    elif command == "pause":
        emit("PAUSED", sequence)
    elif command == "memory_wait":
        emit("PAUSED", sequence)
    elif command == "memory_recovered":
        emit("RUNNING", sequence)
    elif command == "configure":
        attacks = request.get("config", {}).get("attack_slots", [])
        button = next((slot.get("button") for slot in attacks
                       if slot.get("enabled")), "none")
        print(EVENT_PREFIX + json.dumps({
            "level": "INFO", "message": f"fake configured {button}"}),
            flush=True)
    elif command in ("stop", "emergency"):
        if command == "emergency" and ignore_emergency:
            continue
        emit("STOPPED", sequence)
        raise SystemExit(0)
    elif command == "game-close":
        raise SystemExit(0)
