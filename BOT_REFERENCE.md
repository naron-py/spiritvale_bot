# SpiritVale Bot — Feature, Logic, and Recreation Reference

This is the compact operational and architectural reference for the current
SpiritVale bot. It is intended for both a user returning after a long break and an
agent that needs to inspect, repair, or recreate the bot without rediscovering its
core behavior.

For detailed evidence behind unusual rules, read `CLAUDE.md`. That file records the
live-game failures that produced the rules below and is authoritative when changing
code.

## 1. Purpose and operating model

The bot farms monsters in SpiritVale on Windows. It:

- finds valid monsters using read-only game memory;
- temporarily uses red minimap pixels while memory is not ready;
- steers a virtual XInput left stick toward the selected target;
- holds or taps attack, runs a fast spam action, and recasts a timed buff sequence;
- identifies and collects configured ground loot;
- learns walls from observed movement and routes around them;
- can confine farming to an exact recorded circle, polygon, or circle union;
- can reconnect through the login screens;
- displays one live terminal dashboard showing what it sees and why it is acting.

The Python memory path opens the game with read-only process rights. It calls
`ReadProcessMemory`; it does not call `WriteProcessMemory`, inject code, or modify
game state. Character control is external through a virtual X360 controller.
Reconnect automation uses ordinary mouse clicks.

## 2. Safety and control

- The bot starts paused: `START_PAUSED = True`.
- Press **End** from any window to start or pause it.
- `Ctrl+C` exits and releases/centers controller state.
- Pausing releases attack and clears active movement ownership.
- Use the virtual `vgamepad`/ViGEmBus backend. SpiritVale reads XInput, not the
  Arduino Leonardo's generic HID controller.
- The virtual controller must occupy XInput slot 0. Disconnect competing physical
  or Steam-remapped controllers if input appears correct but the character does not
  move.

## 3. Required lifecycle

The target-source lifecycle is load-bearing:

```text
START / RECONNECT
       |
       v
PIXELS while first memory scan is pending
       |
       v
memory scan publishes units and local owner
       |
       v
PIXELS while movement-basis calibration is pending
       |
       v
MEMORY after owner + basis are ready
       |
       +---- disconnect, relog, rebuilt player, or stale world ----+
       |                                                           |
       +---------------- reset old state and repeat <---------------+
```

In exact terms:

1. Starting the script does nothing until **End** is pressed.
2. The background scanner validates cached IL2CPP classes and sweeps for units.
3. During the first scan, normal red-dot minimap targeting can move and attack.
4. The scanner publishes a coherent unit list and resolves the locally owned player.
5. Calibration measures how two stick directions map to horizontal world movement.
6. Once `MemoryEyes.me` and `MemoryEyes.basis` are both valid, memory targeting owns
   normal movement.
7. On disconnect/relog/player rebuild, unit pointers, narrowed caches, owner,
   calibration, targets, and generation-bound scanner state are invalidated.
8. Pixels cover the recovery window; a new scan and calibration restore memory.

Dashboard interpretation:

- `SCANNING FIRST PASS`: no unit pass has published yet.
- `READY - REFRESHING`: the first pass is complete; the scanner thread remains alive
  intentionally for periodic refreshes.
- `Calibration: WAITING`: the scan may be ready, but owner/basis handoff is not.
- `Active source: PIXELS`: pixels won this frame.
- `Active source: MEMORY`: a memory result owns this frame.

Do not use scanner-thread liveness as first-pass status. The thread is persistent.

### Area/calibration deadlock rule

Pixels are deliberately permitted while scan/calibration is pending, including an
`--area` run. Before a basis exists, world-coordinate confinement cannot be
projected onto a stick. Pixels may therefore carry the player outside the recorded
area.

If the player is already outside, owner-only calibration must be allowed. Refusing
all calibration pushes there creates a permanent deadlock: without a basis the bot
cannot calculate the direction home and remains on pixels forever. After
calibration, routed area return takes over immediately. If the player is still
inside but too near the boundary, unsafe calibration pushes remain blocked.

## 4. Target-source arbitration

Memory is the default source. `--minimap` explicitly disables memory targeting.
`--area <name>` needs world coordinates and therefore implies memory unless the user
explicitly supplied `--minimap`, in which case the bot reports that the area cannot
be enforced.

Per-frame behavior:

1. Ask calibrated memory for a monster/loot/return/wander command.
2. A real memory command owns the frame.
3. Temporary memory-unavailable states such as `no unit` and `no monster` leave the
   command unset so pixels can act.
4. Genuine stop states remain neutral and do not fall through to pixels. Important
   examples are `lost`, `invisible`, `no area`, failed construction, committed area
   return/boundary handling, and unsafe final movement rejection.
5. The selected command passes through the final area guard.
6. The actually issued command—not the pre-guard request—is given to wall learning
   and pursuit-clock accounting.
7. The pad receives the final stick and attack state.

A zero stick is a handled memory result. Never return `(0, 0)` merely to mean
"nothing found"; doing so suppresses pixel fallback.

## 5. Memory scanner and class recovery

`memscan.py` contains process discovery, memory-region enumeration, IL2CPP object
walking, class validation, unit classification, loot reading, and local-owner
resolution.

`MemoryEyes` in `minimap_bot.py` owns the background worker and targeting state.

Scanner behavior:

- Validate cached class RVAs by reading the actual class name.
- Never trust an RVA merely because it exists in `il2cpp_rva.json`.
- If a game patch moves classes, stream memory to rediscover class names.
- Recover all due optional classes in one shared search. Separate player and loot
  searches would scan the same multi-gigabyte regions twice and can appear stuck for
  more than five minutes.
- Cache all recovered RVAs in one write.
- Schedule another failed recovery from the completion time of the previous search,
  preventing an overlong search from immediately restarting.
- A normal full `world_units()` sweep is seconds, not minutes. Do not call it in a
  short polling loop.
- Narrowed region caches are dropped after movement or repeated empty sweeps, with a
  rate-limited full-scan backstop.
- Scanner publication and side effects are generation-checked so a pre-relog sweep
  cannot overwrite the new world.

Local-player ownership:

- Resolve the player through FishNet's local `NetworkConnection` ownership chain.
- Try several current unit objects as manager-chain seeds; the first pooled wrapper
  may be stale after reconnect.
- A resolved owner can start calibration even if optional player-class rows are
  temporarily absent.
- Never actuate from a historical/cached player coordinate when the current read
  failed.

Generated cache:

- `il2cpp_rva.json` stores rediscovered class RVAs.
- It is gitignored and safe to delete; the next run searches again.

## 6. Calibration

The bot does not assume world-axis orientation, camera angle, or minimap rotation.
It measures a 2x2 basis mapping stick input to world `(x, z)` travel.

Preferred path:

1. Resolve the locally owned player structurally.
2. Push two independent stick directions.
3. Read the owner's before/after position.
4. Fit the basis and normalize future commands through `stick_for()`.

Fallback path:

- If structural ownership is unavailable, retain the multi-leg fit that scores which
  unit's movement is best explained by all commanded stick legs.
- Do not identify the player as simply "who moved furthest"; another player can walk
  faster during calibration.

Calibration readiness is separate from scan readiness. Memory becomes the active
source only after both owner and basis are usable.

## 7. Memory monster classification

The unit list contains many objects that must not be attacked. A targetable monster
must pass structural and live-state checks:

- classified as a monster controller;
- freshly readable position;
- rendered/visible state;
- health above zero;
- non-empty `MonsterId`;
- not structurally invisible/cloaked;
- not a summoned pet;
- inside the requested farming area's exact admission boundary, if active.

Why all checks matter:

- Pooled/despawned monsters retain old positions and can have full health.
- Rendered monsters without `MonsterId` look real but cannot be damaged.
- `IsVisible` does not mean "not invisible"; invisibility is a separate status flag.
- Pets and monsters can look identical on the minimap, but memory can distinguish
  ownership/summoning structure.

Current-frame data wins. Cached positions can help scheduling but must never drive
from an unreadable current player position.

## 8. Memory target selection and combat

Selection logic:

- Rank targetable monsters by world distance.
- Hold the current target rather than choosing the nearest every frame.
- Prefer FishNet `NetworkObject.ObjectId` as stable identity; retain the managed
  pointer as the live read handle.
- Switch only when another target is clearly nearer by `TARGET_SWITCH`.
- A transient unreadable stable ID does not prove a target changed.
- A positive different ID at the same pointer proves pooled-pointer reuse.
- Give up and temporarily blacklist targets that consume their pursuit/engagement
  budget or are proven walled off.

Combat movement:

- Far target: walk toward it and retain the engagement clock.
- Melee: orbit at a configured standoff rather than stopping or standing directly on
  the monster.
- A zero stick can make the game fall back to keyboard mode and stop attacks landing.
- Standing directly on a monster can leave too little room for the attack animation.
- Orbit direction reverses when the player position stops changing.
- Attack continues during normal chase/orbit and is released for safety/return states.

Pixel mode cannot reliably identify the player's own pet and does not wait for kills
to finish. Those limitations are why memory is primary.

## 9. Pixel/minimap targeting

The pixel path captures a resolution-relative box around the in-game minimap using
`mss` and OpenCV.

Pipeline:

1. Capture `minimap_region(win)`.
2. Threshold red in HSV.
3. Build contour centroids with `find_red_dots()`.
4. Treat the box center as the player's marker.
5. Reject central blobs inside `CONCEAL_PX`.
6. Apply pet/player pairing filters where possible.
7. Keep a target lock to avoid frame-by-frame switching.
8. Detect lack of progress and temporarily blacklist stuck markers.
9. Convert direction to a full-magnitude stick vector.
10. Coast briefly through marker flicker.

Important limitations:

- Red pixels cannot reliably distinguish monsters from the player's pet.
- The player marker is always the calibrated minimap center; do not restore white
  marker detection.
- Stick magnitude is direction-only/full strength. Proportional tilt falls into the
  game's deadzone.
- The pixel path is a startup/recovery safety fallback, not the preferred steady
  state.

Use `python minimap_bot.py --snap` to verify minimap calibration.

## 10. Loot

Loot is read from pooled `LootDrop` objects in memory.

A drop is live only when its synced payload has a non-empty item name. Wrapper
address and old position do not prove occupancy because loot objects are pooled.

Behavior:

- `loot_names.txt` contains case-insensitive name substrings.
- `Card` matches every item name containing `Card`.
- Blank/comment-only configuration means collect every named drop.
- Filtered loot rows are omitted from the dashboard; wanted items remain visible.
- A very nearby drop can outrank a farther monster via `LOOT_FIRST_RANGE`.
- Loot does not interrupt `on it`, `unwedge`, or committed `going back` states.
- Loot at the player's feet can be collected during combat without walking away.
- Loot pursuit time counts only while loot owns the issued movement.
- Uncollectable drops time out and receive a temporary spawn-specific blacklist.
- The spawn key includes wrapper, name, and position so pooled reuse is fresh.

Useful diagnostics:

```text
python memscan.py --loot
python minimap_bot.py --lootlog
```

## 11. Wall learning and routing

SpiritVale uses a Unity NavMesh, but the external Python bot does not call the
in-process NavMesh API. It learns a coarse walk map from read-only observations.

Evidence sources:

- The player's actual movement projected onto the issued stick direction.
- Net progress toward the current issued goal over a time window.
- Moved monsters/players as evidence of traversable floor.

Rules:

- Repeated player positions alone do not indicate a wall; server position updates can
  stall and later jump.
- A fast projection test catches steep/head-on collisions.
- A slower progress-to-goal test catches shallow sliding that gains no useful ground.
- Several sightings are needed before a cell becomes blocked.
- Other units can paint likely floor but never erase a measured wall.
- Only the player's own feet clear a blocked cell.
- Unknown cells are passable but costlier than known floor.
- Routing uses weighted bounded Dijkstra, prefers known floor, prevents diagonal
  corner cutting, and replans at a controlled rate.
- A fast wedge triggers alternating sideways/back escape pushes.
- A route-budget cap degrades to a straight walk; only a genuine sealed dead end
  blacklists the target as walled.
- Wall learning observes the final issued goal and command after area redirection.

`walkmap.json` is gitignored and can be deleted after a map geometry change. The bot
will relearn it.

## 12. Farming areas

Supported geometry:

- exact circles;
- exact polygons;
- circle unions;
- legacy walk-painted masks.

Target admission uses the exact boundary. Player movement uses an inset safe interior
based on `AREA_SAFETY` and validates the entire projected segment, not just its
endpoint. This prevents crossing concave polygon notches or gaps between circle-union
members.

Behavior with `--area <name>`:

- reject new and held monsters outside the exact area;
- reject loot outside the area;
- return through the router if the player is outside the safe interior;
- keep return committed to a safe `home_goal`;
- apply one final world-coordinate stick guard before every pad command;
- wander between committed safe points when there is nothing attackable;
- hold safely if the named area is implausibly far away and likely belongs to another
  map.

The game exposes no reliable map/zone ID, so area names cannot be auto-selected.

### Guided recording

Recommended:

```text
record_farming_zone.bat
```

or:

```text
python minimap_bot.py --record
```

The guided recorder asks in this order:

1. polygon or circle;
2. area name;
3. radius, for a circle.

Recording reads the player's world position from memory and does not drive the pad.
Duplicate names are replaced only after the new recording succeeds.

Direct forms:

```text
python minimap_bot.py --zone polygon <name>
python minimap_bot.py --zone circle <name> --radius 40
python minimap_bot.py --zone clear <name>
python minimap_bot.py --area <name>
```

The recorder and bot must not run simultaneously.

`areas.json` is local/generated and gitignored. Back it up if recorded zones matter.

## 13. Reconnect logic

When enabled with `RECONNECT = True`, the bot recognizes and operates this flow:

1. disconnected modal → click **Ok**;
2. server screen → locate **Southeast Asia (SEA)** by template, never fixed row;
3. character screen → click **Play Character**;
4. wait for a fresh game world;
5. invalidate old memory generation and targeting state;
6. pixels act while the new scan and calibration are pending;
7. memory resumes after the new owner and basis are ready.

Screen detection requires a button plus screen-specific backdrop evidence. A lone
blue-pixel test can mistake gameplay sky/UI for a login screen and click during
combat.

The flow has a repeated-screen limit and diagnostic screenshots so a false positive
cannot click forever. Set `RECONNECT = False` to disable it.

## 14. Dashboard

The terminal dashboard is the primary truth source. It reports:

- configured target mode;
- active source this frame;
- semantic combat/navigation state;
- issued stick and attack state;
- scanner first-pass/refresh status;
- resolved classes and calibration state;
- local character identity;
- cached monster/player/pet counts;
- targetable monster names;
- current monster or loot target;
- wanted loot summary;
- direct/routed navigation and area state;
- warnings and controls.

Use semantic rows together. For example:

- many raw monster objects + no targetable monsters is normal pooled-object filtering;
- `READY - REFRESHING` + `Calibration: WAITING` + `PIXELS` means calibration/owner
  handoff, not an unfinished scanner;
- `direct` means the straight route is clear, not that routing is disabled;
- `wander` under an area means normal idle patrol;
- a stale target name can be a display issue if navigation says `going back`; verify
  actual issued movement before changing target logic.

## 15. Files and responsibilities

| File | Responsibility |
|---|---|
| `minimap_bot.py` | Main loop, vision, pads, arbitration, memory targeting, loot, routing, areas, reconnect, dashboard, diagnostics |
| `memscan.py` | Read-only Windows process memory, IL2CPP/FishNet object traversal, unit/loot/class discovery |
| `farming_zone.py` | Pure circle/polygon geometry and segment checks |
| `zone_recorder.py` | Guided/direct read-only world-coordinate area recording |
| `area_editor.py` | Legacy detached circle planner |
| `loot_names.txt` | Wanted loot name substrings |
| `areas.json` | Local recorded zones; generated/gitignored |
| `walkmap.json` | Learned walls/floor; generated/gitignored |
| `il2cpp_rva.json` | Recovered class RVAs; generated/gitignored |
| `tests/` | Cross-state, dashboard, identity, recorder, and area regressions |
| `CLAUDE.md` | Detailed invariants and evidence; authoritative engineering context |

## 16. Common commands

```text
python minimap_bot.py                       default: memory primary, pixels while pending
python minimap_bot.py --memory              explicitly request memory primary
python minimap_bot.py --minimap             force pixels only
python minimap_bot.py --area <name>         run inside a recorded area
python minimap_bot.py --record              guided polygon/circle recorder
python minimap_bot.py --snap                save annotated minimap capture
python minimap_bot.py --watch               read-only live minimap visualization
python minimap_bot.py --demo                offline bot self-test
python minimap_bot.py --test                blind controller circle test
python minimap_bot.py --buff [hold] [gap]   test one buff sequence
python minimap_bot.py --probe               identify X360 button mappings
python minimap_bot.py --relogin --dry       inspect reconnect screen without clicking
python minimap_bot.py --targetlog           log target changes
python minimap_bot.py --fightlog            log distance and target health
python minimap_bot.py --lootlog             explain loot decisions
python minimap_bot.py --walklog             show wall-sensor measurements
python memscan.py --demo                    offline memory-layer self-test
python memscan.py --units                   classify current world units
python memscan.py --loot                     list live named ground loot
python memscan.py --check <address>          inspect one unit object
```

## 17. Setup and dependencies

```text
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Dependencies:

- `mss`: screen capture;
- `opencv-python`: minimap/login image processing;
- `numpy`: vectors and calibration fits;
- `pygetwindow`: SpiritVale window discovery;
- `vgamepad`: virtual XInput controller;
- `pyserial`: optional Arduino protocol backend.

ViGEmBus is required by `vgamepad`.

## 18. Tuning

Tuning constants are grouped near the top of `minimap_bot.py`. Prefer changing a
constant over adding a new state path.

Important groups:

- minimap box and color thresholds;
- arrival/conceal/flicker/target-lock timing;
- memory ranges, calibration push time, orbit distances, and target switching;
- loot range, priority, pursuit timeout, and ignore duration;
- wall cell size, evidence thresholds, route corridor/budget, and wedge escape;
- area safety margin, lookahead, return timeout, and wander commitment;
- reconnect button/backdrop regions and retry limits;
- attack/spam/buff timing.

Tune from diagnostics and measured live values. Do not weaken structural identity or
safety checks merely to make a symptom disappear.

## 19. Troubleshooting map

### Scanner appears stuck

- `SCANNING FIRST PASS`: first unit pass really has not published.
- `READY - REFRESHING`: scan is done; look at calibration, owner, and navigation.
- Class recovery after a patch may take minutes, but optional classes must share one
  pass.
- Do not repeatedly invoke `world_units()` while diagnosing.

### Pixels never switch to memory

Check dashboard in this order:

1. scanner first pass ready;
2. monster/player/loot classes;
3. local owner/player address;
4. calibration ready;
5. active area and whether the player is outside it;
6. current `Navigation` mode.

A known owner outside an area must be allowed to calibrate, then memory should return
home. After reconnect, verify scanner generation reset, multi-seed owner recovery,
and fresh calibration.

### Bot reports memory but does not move

- Inspect navigation mode; a genuine stop state may be correct.
- Never reinterpret `no unit`/`no monster` as a handled zero stick.
- Check the final guarded stick, not only the vector returned by target selection.
- Verify the virtual pad is XInput slot 0.

### Bot walks toward fake/dead monsters

Verify `real_monster()` still requires rendered + health + `MonsterId` + not invisible
+ not pet. Do not simplify to health or visibility alone.

### Bot presses against a wall

Use `--walklog`, inspect issued goal ownership, and confirm the loaded walk map has the
same cell size. Repeated position reads are not wall proof.

### Bot leaves or sticks at an area edge

Verify whole-segment guarding, post-normalization guarding, routed return, safe
`home_goal`, and that return state was not overwritten before the final pad call.

### Loot is ignored

Use `python memscan.py --loot` and `--lootlog`. Determine whether the drop is absent
from the scanner cache, filtered by `loot_names.txt`, or temporarily blacklisted.

### Reconnect clicks during gameplay

Disable `RECONNECT`, inspect `reconnect_*.png`, and tighten button-plus-backdrop
recognition. Never replace it with one fixed color probe or fixed server row.

## 20. Required verification after changes

Run all of these before claiming a code change is complete:

```text
python -m unittest discover -s tests -v
python minimap_bot.py --demo
python memscan.py --demo
git diff --check
```

For changes to `MemoryEyes.target()`, scanner lifecycle, wall routing, movement
ownership, or area guarding, also create a focused temporary script that calls the
real production method using controlled fakes. Put it under
`%LOCALAPPDATA%\Temp\hermes-verify-*.py`, run it, and delete it afterward.

For a live bug:

- confirm which `minimap_bot.py` PID is running;
- restart after edits;
- inspect the direct source first (live game memory, dashboard, screenshot, area
  JSON, or process state);
- separate physical behavior from a stale dashboard label;
- do not infer current external state from an old conversation.

## 21. Clean-room recreation checklist

If the bot must be rebuilt rather than repaired, recreate it in this order:

1. **Safe actuator**
   - virtual XInput pad abstraction;
   - start paused, global toggle, guaranteed release on pause/exit;
   - wake nudge before button sequences.
2. **Pixel fallback**
   - window-relative minimap capture;
   - HSV red-dot extraction, center exclusion, target lock, full-strength direction;
   - flicker coast and temporary marker blacklist.
3. **Read-only memory foundation**
   - process/module/region enumeration;
   - exact-length guarded reads;
   - verified IL2CPP class slots and streaming class recovery;
   - generated RVA cache.
4. **Unit model**
   - unit sweep and player/monster/pet classification;
   - FishNet local-owner chain and multiple seed attempts;
   - fresh positions, monster liveness, MonsterId, invisibility, stable ObjectId.
5. **Lifecycle**
   - persistent scanner with explicit first-pass publication;
   - generation reset on reconnect/rebuild;
   - pixels while scan/calibration pending;
   - owner/basis gate for memory handoff.
6. **Calibration and memory steering**
   - owner-first two-axis basis fit;
   - multi-leg fallback fit;
   - target hysteresis, orbit combat, timeouts, identity-aware blacklists.
7. **Loot**
   - synced-name occupancy, substring filter, arbitration, pickup, timeout, pooled
     spawn identity.
8. **Routing**
   - projected-progress and net-approach wall evidence;
   - weighted bounded routing, floor preference, wedge escape, persistence.
9. **Areas**
   - exact circle/polygon geometry;
   - exact target admission plus inset segment-safe movement;
   - routed committed return and committed wander;
   - guided read-only recorder.
10. **Reconnect and dashboard**
    - conservative button-plus-backdrop screen recognition;
    - stale-world reset and recovery lifecycle;
    - honest source/scanner/calibration/owner/target/loot/navigation reporting.
11. **Regression suite**
    - cover cross-state conflicts, not only isolated helper outputs;
    - keep both offline demos green.

Do not remove a strange-looking rule until `CLAUDE.md` has been checked for the live
failure it prevents.

## 22. Non-negotiable invariants

- Python process-memory access remains read-only.
- Startup remains paused.
- Memory is primary; pixels are temporary recovery fallback.
- Reconnect invalidates stale world state and repeats scan/calibration.
- Scanner thread liveness is not scan completion.
- Current player position must be readable before memory actuation.
- Horizontal `x == 0` or `z == 0` alone is valid.
- Monster liveness requires all structural checks.
- Stable ObjectId is preferred, but a transient missing ID is not a target change.
- A "nothing found" result must not be encoded as a handled zero stick.
- Melee movement orbits; it does not stand still.
- Unknown walk cells remain passable.
- Other units never erase measured walls.
- Area guarding checks the full final projected movement segment.
- Wrong-map/far-area failure holds safely; it never disables confinement.
- Loot timers and monster timers advance only while their pursuit owns output.
- Generated caches are disposable; recorded area data should be backed up.
