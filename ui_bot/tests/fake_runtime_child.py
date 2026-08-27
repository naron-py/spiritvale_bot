"""Protocol-compatible child used only by ProcessRuntime integration tests."""

import argparse
import json
import sys

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
        "process_id": options.expected_pid, "session_id": options.session_id,
        "bot_state": state.lower(),
    }), flush=True)


print(EVENT_PREFIX + json.dumps({"level": "INFO", "message": "fake ready"}),
      flush=True)
emit("PAUSED", 1)
sequence = 1
ignore_emergency = False
for line in sys.stdin:
    command = json.loads(line)["command"]
    sequence += 1
    if command == "ignore-emergency":
        ignore_emergency = True
    elif command == "crash":
        raise SystemExit(7)
    elif command == "resume":
        emit("RUNNING", sequence)
    elif command == "pause":
        emit("PAUSED", sequence)
    elif command in ("stop", "emergency"):
        if command == "emergency" and ignore_emergency:
            continue
        emit("STOPPED", sequence)
        raise SystemExit(0)
    elif command == "game-close":
        raise SystemExit(0)
