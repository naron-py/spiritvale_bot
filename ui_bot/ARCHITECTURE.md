# UI Architecture and Failure Contract

## Boundary

The existing SpiritVale terminal implementation is a protected dependency. The UI
does not copy its combat loop and does not modify its source, requirements, offsets,
entry point, controller classes, or global End-key behavior on disk.

```text
Qt widgets (main process)
    │ commands / immutable snapshots
    ▼
ProcessRuntime (asynchronous QProcess)
    │ newline JSON over stdin/stdout
    ▼
runtime_child (isolated Python child)
    │ runtime-only adapters
    ▼
unchanged minimap_bot.main()
    ├── read-only MemoryEyes / memscan
    ├── pixel fallback
    ├── target + loot arbitration
    ├── area guard + routing
    └── sole VirtualPad owner and finally: pad.close()
```

## Modules

- `model.py`: frozen snapshots, validation, all failure codes and policies.
- `app_controller.py`: idempotent UI state machine and emergency latch.
- `runtime.py`: asynchronous process supervisor and deterministic demo runtime.
- `process_discovery.py`: read-only Windows PID discovery with no memory handle.
- `runtime_child.py`: process-local command and snapshot adaptation.
- `config.py`: schema validation, temporary write, fsync, backup, atomic replace.
- `zone_editor.py`: polygon validation and terminal-compatible atomic persistence.
- `widgets/world_view.py`: cached QGraphicsScene renderer.
- `pages.py`: five page widgets; hidden pages receive no periodic updates.
- `main_window.py`: composition, signals, recording workflow, bounded activity log.
- `__main__.py`: startup, fatal exception hook, demo/screenshot harness.

## States

The UI keeps three independent state axes:

```text
ConnectionState:    DISCONNECTED -> CONNECTING -> CONNECTED | ERROR
AutomationState:    IDLE -> RUNNING -> PAUSED -> RUNNING
                      ^        │          │
                      └- STOP -┴----------┘
ZoneRecordingState: INACTIVE -> RECORDING -> READY | INVALID

ANY AUTOMATION STATE -> SAFE_STOP / EMERGENCY_STOP (latched)
```

Connection monitoring does not imply automation, and recording does not start
targeting, movement, combat, or memory writing. Losing the game connection while
recording preserves the draft and disables only position sampling until reconnect.
Discovery starts during UI construction and polls with capped exponential backoff.

Duplicate Start, Pause, Stop, and Emergency Stop requests are safe no-ops. Start is
blocked while the emergency latch is set.

## Snapshot contract

Snapshots are immutable and schema-validated before use. They carry only copied
values:

- sequence/timestamp/scan version, process PID/session token, and independent
  connection/automation status;
- current player X/Z;
- one canonical stable-ID-deduplicated entity tuple per scan, including copied
  liveness, pointer validity, current-target, and shared inside-zone results;
- current target and distance;
- polygon/circle/cell zone geometry;
- route and bounded player trail;
- terminal dashboard data and status text.

Non-finite or implausible coordinates, malformed JSON, invalid state values, and
out-of-order world updates are rejected. A malformed worker snapshot enters the
explicit malformed-snapshot failure path; it is never rendered partially.
Snapshots from an old PID/session are discarded and freshness timing resets whenever
a new monitor session begins.

## Thread/process safety

- Game work is isolated in the bot child process.
- Qt's QProcess readiness signals prevent blocking pipe reads.
- No paint/event callback opens a process, follows a pointer, or sends movement.
- Every widget update runs on the Qt UI thread.
- The world view never stores game-memory handles, only frozen values.
- Hidden pages are not refreshed; switching pages applies only the latest snapshot.
- Logs cap at 1,000 visible blocks and rotate at 1 MB × five backups.

## Cleanup

Normal Pause and Stop toggle the original loop to neutral controls while its
read-only scanner and immutable snapshots continue. Emergency Stop and window
shutdown raise a KeyboardInterrupt-compatible request only at the loop boundary,
so the existing `finally: pad.close()` runs. Parent termination occurs only after
a cleanup timeout. Window close invokes the same emergency path and waits for the
child.

## Configuration

UI configuration is separate from terminal configuration. The save path is:

```text
payload -> settings.json.tmp -> flush/fsync -> backup old file -> os.replace
```

Invalid settings are rejected before the good file is touched. A corrupt primary
loads one valid backup, otherwise safe defaults. UI target source and selected area
become the unchanged terminal command-line options on the next child start.

Zone saving uses the same atomic pattern and stores selected name plus coordinates
in `areas.json`; this selection is authoritative over a stale UI settings copy.
Existing areas and both supported polygon schemas are preserved. Too-few, duplicate, degenerate,
self-intersecting, non-finite, and impossible points are rejected before save.

Saved-zone presentation is independent from connection state: `NO SAVED ZONE`,
`ZONE LOADED, GAME DISCONNECTED`, `ZONE ACTIVE`, and `INVALID SAVED ZONE` are
distinct. Disconnecting never clears valid loaded geometry.

## Failure handling

`model.FailureCode` enumerates every required failure category. Every enum member
must exist in `FAILURE_POLICIES`; tests fail if one has no detection method, user
message, safe state, input-release action, bounded retry policy, or traceback log.

The common response is fail closed:

1. stop movement and attack through the runtime emergency path;
2. clear the active UI target by replacing the snapshot with a safe snapshot;
3. enter `PAUSED`, `DISCONNECTED`, or `SAFE_STOP` as specified;
4. show an actionable message;
5. record full context and traceback;
6. retry only where the policy explicitly allows a bounded retry.

Expected no-target states are observation-only, never endless movement. Invalid
memory, controller failures, worker exceptions, malformed snapshots, and forced
shutdown all request release of every input.

Process completion is classified from worker purpose, expected lifetime,
`stop_requested`, exit code, QProcess exit status, and last-valid-snapshot time.
Code 0/NormalExit completes one-shot tasks successfully, requested persistent exits
are normal, and unrequested normal persistent exits restart discovery with capped
backoff. Non-zero or CrashExit is a real latched failure.

## Verification

```text
# UI unit and offscreen integration tests
set QT_QPA_PLATFORM=offscreen
.venv\Scripts\python.exe -m unittest discover -s ui_bot\tests -v

# Rendered deterministic smoke test
.venv\Scripts\python.exe -m ui_bot --demo --screenshot ui_bot_demo.png

# Existing terminal regression gates
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe minimap_bot.py --demo
.venv\Scripts\python.exe memscan.py --demo

git diff --check
```

`original_manifest.json` records SHA-256 hashes for all pre-UI project files except
mutable runtime caches. `verify_originals.py` proves those protected dependencies
remain byte-for-byte unchanged.
