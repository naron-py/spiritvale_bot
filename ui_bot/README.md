# SpiritVale Farm Bot UI

A PySide6 desktop UI around the existing terminal bot. The terminal source is
imported unchanged in a child process; it remains the sole owner of memory reads,
targeting decisions, movement commands, and controller cleanup.

## Install

From the repository root:

```text
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r ui_bot\requirements-ui.txt
```

## Run

Double-click:

```text
run_ui_bot.bat
```

Or run explicitly:

```text
.venv\Scripts\python.exe -m ui_bot
.venv\Scripts\python.exe -m ui_bot --demo
```

Demo mode is deterministic and does not open SpiritVale, process memory, or a
controller. It exercises connection/memory status, entities, target selection,
zone rendering, trail/path layers, activity logs, and every control state.

## Safety

- Automation starts stopped; the child attaches in paused monitor-only mode.
- The UI discovers SpiritVale automatically. When the game is absent it shows
  `Waiting for game…` and retries with capped backoff; reopening the game attaches
  a new PID/session without requiring Start.
- Read-only player/entity scanning continues in IDLE, RECORDING, PAUSED, and RUNNING.
- Start, Pause, Stop, and Emergency Stop are idempotent.
- `Ctrl+Shift+F12` is the global-in-window emergency shortcut.
- Emergency Stop latches; Start remains blocked until **Reset E-Stop**.
- Reset E-Stop restarts read-only discovery while leaving automation idle.
- The real bot runs in a separate child process.
- Pause/Stop are consumed by the unchanged main loop at its 20 Hz boundary,
  release inputs, and leave read-only monitoring active.
- Emergency Stop and window shutdown reach the unchanged `finally: pad.close()` path.
- A forced process kill is a delayed fallback only.
- UI rendering uses immutable cached snapshots; no paint callback reads memory.
- Window close requests emergency cleanup before exit.

## Runtime integration

`runtime_child.py` performs process-local adaptation without editing terminal
files:

1. imports `minimap_bot`;
2. replaces the End-key edge with a bounded stdin command gate;
3. replaces terminal text rendering with a JSON snapshot publisher;
4. calls the original `main()` in its safe paused state and starts its existing
   read-only `MemoryEyes` scanner without enabling automation;
5. lets the original loop retain targeting, safety, movement, reconnect, and pad
   ownership;
6. exits through the original controller cleanup.

The parent uses asynchronous `QProcess` signals. It never blocks the UI thread on
pipe reads, process waits, screenshots, or memory access.

Worker purpose, expected lifetime, requested-stop state, exit code/status, and
last valid snapshot time are retained for exit classification. A successful
one-shot is success, requested monitor shutdown is normal, and an unrequested
normal monitor exit returns to discovery. Non-zero and crash exits latch SAFE_STOP.

## Pages

- **Dashboard:** status cards, live world view, target/zone panel, activity log.
- **Targeting:** source, scan state, classification, and selection explanation.
- **Farming Zone:** live polygon recorder using fresh cached player positions,
  including before Start and while automation is stopped or paused.
- **Combat:** neutral/held input monitor and cleanup contract.
- **Settings:** target mode, area, reconnect, world limits, rotating logs, and a
  table proving every enumerated failure has a safe state.

## Generated state

- `ui_bot/state/settings.json` — atomic UI settings.
- `ui_bot/state/settings.json.bak` — previous valid settings.
- `ui_bot/logs/ui.log` — 1 MB rotating log with five backups.
- `areas.json` — shared terminal-compatible area data; UI saves through an atomic
  backup-preserving adapter. The selected name and geometry are written together;
  both legacy `polygon` and current `shape=polygon`/`points` records load on reopen.

## Tests

```text
set QT_QPA_PLATFORM=offscreen
.venv\Scripts\python.exe -m unittest discover -s ui_bot\tests -v
```

See `ARCHITECTURE.md` for module boundaries, failure handling, and verification.
