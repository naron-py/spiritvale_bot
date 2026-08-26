# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A combat bot for the game **SpiritVale** (Windows). It drives a gamepad left stick
toward the nearest monster while holding the attack button and recasting a d-pad
buff on a timer.

It picks targets two ways. The **memory** path reads the game's unit list and knows
what each thing *is* — monster, player, or pet — which the screen cannot tell. The
**pixel** path finds red dots on the minimap and is the fallback: it runs during the
first background sweep, and whenever memory targeting is unavailable. Memory access
Memory access is read-only (`ReadProcessMemory`); nothing is written and nothing is injected.

## In-process BepInEx migration (approved 2026-08-23)

The user explicitly approved migrating the bot to an in-process C# + BepInEx 6
plugin, accepting the ToS/ban risk. This **overrides** the read-only / no-injection
constraint above for this project: the C# bot runs inside the game process via
BepInEx + Il2CppInterop and reads (and, where actuation needs it, drives) game
state directly. The Python bot remains the reference behavior and the fallback
actuator during migration. See `MIGRATION_AUDIT.md` for the full audit and the
verified IL2CPP data model. Game: Unity 6 (6000.0.64f1), IL2CPP, FishNet, no
client-side anti-cheat.

## Commands

```
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

python minimap_bot.py              # run, memory primary; pixels while recovering
python minimap_bot.py --memory     # run, explicitly request memory primary
python minimap_bot.py --minimap    # run, red dots only, never memory
python minimap_bot.py --port auto  # run, Arduino Leonardo serial backend
python minimap_bot.py --demo       # the test suite (see below)
python minimap_bot.py --snap       # dump minimap_snap.png with detections drawn
python minimap_bot.py --test       # walk a blind circle: isolates pad vs vision
python minimap_bot.py --buff [hold] [gap]   # fire buff sequence once
python minimap_bot.py --probe      # press every X360 button in turn, named
python minimap_bot.py --walklog    # what the wall sensor sees, every frame
python minimap_bot.py --record         # guided polygon/circle recorder; asks name, replaces duplicates
python minimap_bot.py --circle <name> <radius>  # exact round area centred where you stand
python minimap_bot.py --zone polygon <name>  # memory-coordinate hotkey recorder
python minimap_bot.py --zone circle <name> --radius 40
python minimap_bot.py --zone clear <name>
python minimap_bot.py --place-circles <name> [radius]  # legacy top-down planner only
python minimap_bot.py --area <name>    # run confined to a recorded area
python minimap_bot.py --lootlog    # why a drop was or was not walked to

python memscan.py --demo           # memory layer self-check, no game needed
python memscan.py --units          # list what the unit sweep classifies right now
python memscan.py --loot [seconds] # ground loot; with seconds, which slots are fresh
python memscan.py --check <addr>   # inspect one unit object
```

There is no test framework. `demo()` in `minimap_bot.py` **is** the test suite: an
assert-based self-check on synthetic images plus a mocked serial port, runnable
with no game, no gamepad, no board. Extend it when adding vision or protocol logic;
`python minimap_bot.py --demo` must stay green. Arg parsing is hand-rolled `sys.argv`
scanning at the bottom of the file — no argparse in `minimap_bot.py`.

## Architecture

`minimap_bot.py` is the bot; `memscan.py` is the memory layer it sits on.

`minimap_bot.py`, four layers:

1. **Vision** — `find_red_dots` runs an HSV threshold + contour centroids over an
   `mss` grab of `minimap_region(win)`. The capture box is `MINIMAP = dict(cx, cy,
   r)` as *fractions* of the client area, so it survives resolution changes. The
   character is assumed to sit at the box centre — the game pins it to the middle
   of its minimap — so `MINIMAP` cx/cy is load-bearing and must be calibrated with
   `--snap`. `pick_target()` holds this rule and is shared by `main()` and
   `--watch`, so the live view cannot disagree with what the bot chases. Four
   stateful helpers sit behind it, all reset together when the bot is toggled:
   `PetFilter` (drops other players' pets), `TargetLock` (keeps one marker instead
   of re-picking the nearest each frame), `StuckWatchdog` (spots a target that is
   not getting closer) and `TargetBlacklist` (ignores it for `TARGET_IGNORE_S`).
2. **Control** — `stick_vector` converts a screen delta to a stick vector. It is
   **direction-only at full magnitude**; a minimap pixel is many world metres, so
   proportional tilt lands inside the game's own deadzone. `main()` holds the last
   heading for `LOST_HOLD_S` when a dot flickers out ("coasting"), and treats a
   dot vanishing under the arrow as arrival ("concealed").
3. **Memory targeting** — `MemoryEyes` wraps `memscan.py`. A background thread
   sweeps the heap for units and caches the membership list (`MEM_REFRESH_S`);
   positions are read fresh per frame, so a monster is chased where it is now.
   `calibrate(pad)` learns two things at once by pushing the stick six ways:
   which unit is ours, and the 2x2 `basis` mapping a stick push to world travel —
   so no world axis, camera angle or minimap rotation is ever assumed.
   `target(now)` returns a stick vector and reports a `mode` the status line
   prints: `chasing`, `far`, `on it`, `gave up`, `no monster`, `lost`, `no unit`.
   Everything it depends on is checked rather than trusted — see the constraints
   below.
   Loot rides on the same layer: the background sweep also calls
   `world_loot()`, `pick_loot(now)` returns a stick toward the nearest drop
   within `LOOT_RANGE` whose name passes `wanted_item()` (`LOOT_NAMES`), and
   `LOOT_BUTTON` (left trigger) is tapped on arrival. Loot is only consulted
   when the monster path has nothing or is `far` -- never mid-fight.
4. **Walk map** — `WalkMap` is a coarse grid over world (x, z) recording where the
   character can and cannot go. Walls come from our own movement: the cell ahead
   is blocked when travel *projected onto the commanded direction* stays under
   `WALK_BLOCK_PROGRESS` for `WALK_BLOCK_FRAMES` (`observe()`, fed the actually
   issued stick by `observe_move()`), or when a `WALK_STUCK_S` window brings us
   less than `WALK_STUCK_MIN` closer to the goal (`_creeping()`, the only sensor
   that catches a shallow slide). Floor comes from the background sweep —
   `paint()` takes every unit that moved since the last one, since they walk the
   same navmesh we do. `MemoryEyes.route_to()` keeps the straight line unless it
   crosses a known wall, then runs a weighted search that prefers floor and
   steers at a waypoint; a goal with no route at all is blacklisted (`walled`).
   Both channels persist to `walkmap.json` (gitignored) from the background
   thread. `PATHFIND = False` turns all of it off; `--walklog` shows the sensor.
5. **Farming areas** -- `farming_zone.py` contains pure horizontal-world geometry;
   `Area` adapts exact circles, polygons, circle unions, and legacy walk-painted
   masks to the bot. `zone_recorder.py` samples the player's read-only memory
   position under configurable global hotkeys, detects X/Z versus X/Y from the
   sampled motion, and saves X/Z zones to `areas.json`. SpiritVale's movement
   model is X/Z; an unexpected X/Y detection is reported and not saved, and an
   externally supplied X/Y zone is refused rather than silently run as X/Z. The
   detached `--place-circles` canvas is only a planner and cannot align to the 3D
   character.
   With `--area <name>`, only alive hostile monsters whose fresh positions remain
   inside are targetable. A safe-interior margin, routed return, and final stick
   guard keep the player inside. Without `--area`, all of this remains inert.
6. **Pad backends** — `VirtualPad` (vgamepad/ViGEmBus, XInput) and `ArduinoPad`
   (serial to a Leonardo). Duck-typed, same methods: `stick(sx, sy, attack)`,
   `tap_dpad(name, hold)`, `tap_trigger(name, hold)`, `close()`. Pick a backend by adding a class with those
   three methods; nothing else in the file knows the difference.

`memscan.py` is the memory layer: region enumeration, the heap sweep
(`world_units`), the IL2CPP object walk, and class resolution. Field offsets and
`TYPE_RVA` sit in one block at its top. It is read-only by construction — it opens
the process with `PROCESS_VM_READ` and never calls `WriteProcessMemory`.

Tuning constants all sit in one block at the top of `minimap_bot.py`. Prefer
adjusting them over adding code paths.

## Hard-won constraints — do not "simplify" these away

### Memory targeting

- **`IsVisible` does not mean "not invisible".** The game's
  `BaseUnitController.get_IsInvisible()` reads
  `StatusComponent.Flags.Invisible` (`unit +0x140 -> status +0x188 -> SyncVar
  value +0x74`, mask `0x20`). A cloaked monster can retain `MonsterId`, health,
  and the `IsVisible` byte while attacks cannot hit it. `real_monster()` rejects
  that structural flag; when one is nearby and nothing attackable remains,
  `MemoryEyes.target()` reports `invisible`, releases attack, and returns a
  handled zero stick so the pixel fallback cannot immediately reacquire the same
  red marker. The short liveness TTL admits it when the flag clears; do not
  replace this with display inference or a permanent blacklist.
- **A monster with no `MonsterId` cannot be damaged.** It is a real
  `MonsterController`, rendered, with a full health bar -- it passes every
  liveness test there is. Measured: 232 rendered-and-alive monster objects near
  the character, only 32 carrying an id, against 26 red dots on the minimap.
  Because they sit inside `MEM_ARRIVE` the bot pinned to `on it` with a zero
  stick, swung for `MEM_ENGAGE_MAX_S`, gave up, and started on the next one --
  which from outside is a bot that will not move and walks back when you drag
  it away. `real_monster()` requires the id; `worth_fighting()` is the weaker
  monster liveness test.
- **Most of the unit list is not there to be fought.** Pooled and despawned
  monsters keep their last position *and get their health reset to full*, so they
  are indistinguishable from a healthy monster standing still. Measured: 516
  monster entries, 468 whose position had not changed in 2 seconds. Without
  `worth_fighting()` the bot parks in a pile of them, fights each for
  `MEM_ENGAGE_MAX_S`, gives up, takes the next from the same pile — and walks
  straight back if you drag the character away. The test is *rendered*
  (`IsVisible`) **and** health above zero; neither alone is enough.
- **Our own unit is read from the local connection, not searched for.**
  `local_player()` walks any unit -> TransportManager -> NetworkManager ->
  ClientManager -> the local NetworkConnection -> the one NetworkObject it owns
  -> its PlayerController. Verified live: the unit it names tracked the
  character at 14.3 units/s while walking and 0.00 while standing still. It
  starts from a unit on purpose -- every unit is a NetworkBehaviour and carries
  the managers, so no extra class has to be resolved. That matters because
  `ClientManager` has **no slot in GameAssembly.dll to cache** (checked to a
  0x20000000 span), so looking it up by name would cost minutes on every run
  rather than once per patch.
- **Calibration still has to walk, but only for the basis.** Knowing which unit
  is ours removes half its job; what a stick push does to our position depends
  on the camera angle and cannot be read from the unit list. With the owner
  known it takes two legs instead of six (`pushes` in `calibrate()`), and
  `stick_for()` normalizes, so only the rotation matters, never the scale.
- **The six-leg fit is still the fallback, and must stay.** If the walk to our
  unit comes back empty, `pick_me()` runs exactly as before. What follows is
  why it works that way:
- **Our own unit is identified by fit, never by who moved furthest.** On a busy
  map another player simply out-walks a 0.7s push, so the biggest-mover rule
  locked onto them and then threw every later leg away as "not us", failing
  calibration outright with a healthy character standing there. `pick_me()`
  scores each unit by how well one 2x2 basis explains all its legs: our travel is
  a linear function of the stick, a passer-by's is not, whatever their speed.
- **Player visibility/health is not a stop signal.** Reusing the monster
  `worth_fighting()` check on our own unit produced false `DEAD` states while the
  character was alive, ending unattended grinds. As long as our position remains
  readable, targeting continues. Monster liveness still uses `real_monster()`;
  `MEM_ARRIVE = 2.5` remains verified killing distance.
- **In melee the bot circles the target; it neither stands still nor stands on
  it.** Two separate findings force this. A stick of exactly zero drops the game
  back to keyboard mode and the held attack silently stops landing (measured: hp
  frozen at 37502 for dozens of frames while the bot reported `on it`). And the
  push that replaced the zero only cancelled itself out frame to frame, so the
  character stayed jammed against the monster -- at that range the game gives no
  attack at all, it needs room to swing. The orbit answers both: it always moves,
  and it holds `MEM_ORBIT_MIN`..`MEM_ORBIT_MAX` of standoff. Those two are a
  calibration knob, not a measurement -- `--fightlog` prints distance against
  target health every frame, and is how to set them. `_orbit_way()` reverses the
  circle when *our own position* stops changing; the radius cannot report a wall,
  since holding the radius constant is the whole job.
- **A far target keeps the engagement clock.** Clearing it each frame meant the
  give-up timer never fired and an unreachable monster could be walked at forever.
- **Held target identity prefers FishNet ObjectId, not only the managed pointer.**
  Every unit is a `NetworkBehaviour`: `unit +0x30` is its `NetworkObject`, and
  `NetworkObject +0x44` is the signed `ObjectId`. The latter offset was verified
  from the native getter (`mov eax,[rcx+44h]; ret`). A pointer is still required
  as the read handle, but an unchanged ObjectId preserves the engagement clock
  across wrapper movement and detects pooled pointer reuse. Pointer fallback
  blacklists record the ObjectId that owned the address, so a positive different
  ID does not inherit the old spawn's ignore interval. `MonsterId` is only a
  species string and must never be treated as a unique entity identity.
- **Never return a zero stick for "nothing to do".** `main()` treats a zero stick
  as handled and does not fall through to the pixel path, so the bot stands
  still. That is why no monster within `MEM_RANGE` walks to the nearest one
  anywhere (`far`) rather than returning nothing.
- **One empty position read is not a death.** Tearing down `me`, `basis` and the
  caches on the first miss cost a whole run — the bot went silent until it was
  restarted. `MEM_LOST_FRAMES` consecutive misses are required. Historical
  `seen_at` coordinates remain useful for staggered far-unit scheduling, but an
  attempted read that fails is absent from the current snapshot; in particular,
  the bot never actuates from a cached player coordinate.
- **A horizontal world-axis coordinate of zero is valid.** Maps can cross
  `x == 0` or `z == 0`; only a pair with both horizontal coordinates effectively
  zero is recycled memory. Requiring both axes to be nonzero loses a live player
  and eventually tears calibration down.
- **Every mode assignment must be honest, including the early returns.** Leaving
  a stale `mode` made a bot with no unit at all report `chasing` while motionless,
  and sent the investigation to the wrong place.

### Walking round walls

- **There is no grid in this game, and the question is settled — do not reopen
  it without new information.** Checked against the shipped
  `SpiritVale_Data\il2cpp_data\Metadata\global-metadata.dat`, which carries
  every IL2CPP class and field name as plain ASCII. A* Pathfinding Project is
  absent outright (no `AstarPath`, `GridGraph`, `GridNode`, `RecastGraph`,
  `GraphNode`), as are `Pathfinder`, `MapGrid`, `GridManager`, `CollisionMap`,
  `ObstacleMap`, `WalkableArea`. Every `Grid`/`Tile` name that *is* there
  belongs to something else: FishNet's `HashGrid` observer spatial hashing,
  MongoDB `GridFS*`, Unity UI `GridLayoutGroup`, Unity 2D `ITilemap`.
  Walkability is a **Unity NavMesh** — the game's own `_App` helper calls
  `SnapToNavMesh`, `TryGetNavMeshPosition`, `GetNavMeshManager`, and
  `com.unity.ai.navigation` ships with it. Two ways to use that were rejected:
  the polygons are native Detour structures inside `UnityPlayer.dll`
  (undocumented, moves with the Unity version, days of work), and querying it
  properly means calling `CalculatePathInternal` in-process, which needs
  `VM_WRITE` and injected code and would end this bot's read-only guarantee.
- **So the walkable area is learned, from two readings that cost nothing.** Our
  own position against the stick we sent says where a wall is; every *other*
  unit's position says where floor is, because monsters and players walk the
  same navmesh and the sweep already carries hundreds of them.
- **A repeated position is the feed hiccuping, not a wall, and counting frames
  can never tell them apart.** `UNIT_POSITION` is `_lastValidPosition` --
  server-validated, not the live transform -- and it repeats for several frames
  at a time as a matter of course. Measured at 20 Hz: walking by hand, 4% of
  frames repeated with one run of **8**; with the bot driving, runs of 3, 4, 5,
  6 *and* 7 inside a single 8 s window. `WALK_BLOCK_FRAMES` was 6, so that is
  two fake walls every 8 seconds, ~15 a minute, stamped on open ground -- and
  from outside it is a bot that walks three steps, turns away from nothing, and
  repeats forever, which is exactly how it was reported. Raising the number
  cannot fix it: a slow feed and a wall both read as "did not move". So
  `observe()` lets **neither** sensor judge on a repeat -- the slow one is
  fooled just as badly, since a stall spanning its window leaves both endpoints
  holding the same value and a walking character reads as gaining nothing.
  Four things were measured and ruled out first, all
  cheaper to re-check than to re-derive: the read path is healthy (36 units
  moved in the same 8 s), `local_player()` has the right unit (51.4 units of
  travel while walking), holding attack does **not** root the character (0%
  stalled held, and it is the *smoother* leg), and a zero stick does **not**
  strand the game in keyboard mode (0-frame lead-in freeze).
- **Being jammed cannot be timed off repeated reads either, and the threshold
  version was tried and failed.** `WALK_STALL_S = 1.0` was set against a worst
  measured stall of 8 frames; a loaded run then produced runs of 15, 17 and 26
  frames (1.3 s) with the feed at 12.4 Hz, and the bot called every one of them
  a jam -- same fake walls, new sensor, and `--walklog` filled with `stalled
  1.0s ... jammed` while the target distance kept changing, which is proof
  things were moving. Any such limit races a feed that slows under load. What
  actually separates the two: a stall *ends* with the position jumping to where
  the character walked, a jam ends where it started. So `_jammed()` measures
  **displacement across `WALK_JAM_S`** and does not care how many samples
  arrived.
- **Progress, not speed, is what a wall takes away.** The first version marked a
  wall when per-frame travel fell under a floor — and learned almost nothing,
  because Unity slides a character along the collider it is pushed into, so
  travel stays near full pace while no ground is gained. The honest test is the
  projection of actual travel onto the direction we asked for
  (`WALK_BLOCK_PROGRESS`). Measured live: `0.00` stuck against `0.81` walking
  free. `--walklog` prints that number every frame and is how it is set.
- **That fast test only catches a steep hit, and raising its limit was
  rejected.** Against 0.81 free walking a slide reads `0.81·cos²θ` for θ into
  the wall: 0.00 head-on and 0.20 at 60° (both caught), but 0.41 at 45° and 0.61
  at 30°, which look exactly like ordinary walking. Raising the limit to 0.45 to
  catch them would also fire on slow ground, on a monster body-blocking us, and
  through every corner — writing walls onto open ground. A shallow slide is also
  often *right*: it is how a character rounds a corner.
- **So the second sensor asks "am I getting closer", not "am I sliding".**
  `_creeping()` judges a `WALK_STUCK_S` window: less than `WALK_STUCK_MIN` of net
  approach means a wall, marked toward the goal. Free walking covers ~14 units in
  that window against a bar of 1.5, so it fires only when genuinely trapped. Two
  things it must keep doing: measure **both** distances against the goal's
  position *now* (or a fleeing monster reads as a wall), and restart the window
  rather than judge when the goal jumps more than `WALK_GOAL_JUMP` (or the
  monster we just turned toward gets a wall drawn in front of it).
- **A router cannot free a character the physics has jammed, and one cell is not
  a wall.** Measured live, wedged against a rock: the bot marked the *same*
  single cell for minutes. It never moved, so it could never learn a second one;
  dodging 1.5 units at 2 units' range bends the heading by 25°, so the route came
  back effectively unchanged and it pushed the same rock again — stick identical
  frame after frame. Two answers, both required. `_wall()` blocks a **fan**
  (`WALK_BLOCK_ARC`, two ranges deep, three ways wide) so a route cannot sidestep
  an obstacle by one cell, skipping the cell we occupy since `free()` clears it
  anyway. And a wedge — which only the fast sensor can mean, since it says we did
  not move at all — fires `wedge_off()`: a fixed sideways-and-back push
  (`WALK_ESCAPE_TURN`) for `WALK_ESCAPE_S`, **alternating sides**, so a corner
  that defeats one way out is escaped the other. `WALK_ESCAPE_GIVEUP` escapes on
  one monster and it is blacklisted as `walled`.
- **On a route, the slow sensor's goal is the waypoint, not the monster.** A
  detour round a big wall closes no distance on the monster for seconds at a
  time, and judging against it there writes a false wall across the way round
  that is working. `pick_loot()` also runs when it *loses* the arbitration, so
  `observe_move()` is told which goal actually got the stick (`on_loot`).
- **Floor from other units is evidence, not proof, and must never clear a
  wall.** A cell is 1.5 units wide and a monster on the far side of a thin wall
  shares its edge; letting that erase a wall we measured gives a
  mark-clear-mark ping-pong. Only our own feet (`free()`) clear a blocked cell.
  For the same reason only units that **moved** since the last sweep count — a
  pooled monster keeps its last position and full health, and one parked inside
  scenery would paint floor that is not there.
- **A cell never visited must route as passable.** Treating unknown as solid
  means a fresh map can only ever walk inside the room it started in, which is
  worse than no pathfinding. Only cells proven blocked are impassable, and only
  those are saved.
- **Walls are only learned while walking somewhere** (`chasing`, `far`, `loot`).
  Standing still is *correct* in every other mode — the orbit holds position on
  purpose, a dead or lost unit sends no stick — and marking from those states
  fills the map with fiction centred wherever the bot happened to stop.
- **One sighting is not a wall.** Another player or a monster in a doorway reads
  exactly like stone. `WALK_BLOCK_HITS`, `WALK_DECAY_S`, and clearing any cell
  the character later stands in are what stop bodies from sealing the map. A
  wrong mark that nothing corrects is worse than no map at all.
- **Route failure returns the target, never a zero stick.** `main()` reads a
  zero stick as handled and never falls through, so an unreachable goal must
  degrade to a straight walk. The one exception is a goal proven *walled off*:
  `route()` sets `capped` to say whether it ran out of budget or of map, and
  only a genuine dead end (`eyes.sealed`) blacklists the monster and reports
  `walled`. Running out of corridor must never do that — the wall may simply be
  longer than the search.
- **The search is bounded to a corridor round the straight line** (`WALK_PAD`).
  Unbounded it spreads through unknown space in every direction: measured at
  9.7 ms against a 50 ms frame, ~3 ms bounded. It replans at most every
  `WALK_REPLAN_S`, and `crossed()` — the per-frame question — is 1 µs.
- **Steps are weighted (Dijkstra), not free (BFS).** Floor costs
  `WALK_FLOOR_COST`, unknown `WALK_UNKNOWN_COST` — preferred, not forbidden, or
  the bot could never explore. Distance has to stay in the cost: an earlier
  0-1 BFS made proven floor free, every route through it cost the same, and a
  12-unit trip came back as a 170-point snake instead of 14.
- **Routing does not get its own `mode`.** It sets `eyes.routing` and prints a
  `~` instead. `mode` is read by the loot arbitration (`far` gives loot its
  turn) and by the wall learner, and a fifth value silently changed both.

### Choosing the target source

- **Memory is the default; pixels are its temporary safety fallback.** Memory
  knows what each unit is and is therefore the preferred source. Pixels cover
  startup, recalibration, scanner recovery and stale offsets. A sustained
  contradiction (pixels see targets while calibrated memory says `no monster`)
  invalidates the narrowed heap cache and requests a rate-limited full sweep.
  `--minimap` is the explicit opt-out. `targeting_mode()` holds the rule and is
  asserted in `demo()`.
- **`--area` implies `--memory`, and an explicit `--minimap` still wins.** A
  recorded area is world coordinates and the screen cannot say where in the
  world anything is, so there is nothing for the fence to test on the pixel
  path -- `main()` says the area is being ignored rather than pretending to
  confine the bot. Silently overriding a flag the user typed is worse than
  refusing it.

### Farming areas

- **Nothing in the game says which map you are on.** Checked against both files
  and `il2cpp_rva.json`: the memory layer resolves `MonsterController`,
  `PlayerController`, `SummoningComponent` and `LootDrop`, and nothing else.
  There is no scene name, zone id or map id anywhere. So an area cannot be
  auto-selected and has to be named on the command line -- and `--area` pointed
  at the wrong map puts "home" thousands of units away. Past `AREA_ABANDON` the
  bot reports the likely wrong map and holds a zero stick with attack released;
  it never disables confinement or hands actuation to pixels. `AREA_RETURN_MAX_S`
  retargets an inset-safe home point while keeping the fence enabled.
- **Target admission is the exact boundary; player motion uses a safe interior.**
  New and held monsters are rejected the moment their freshly read position is
  outside. `safe()` applies `AREA_SAFETY` inward for circles/polygons and uses the
  legacy mask core. Crossing into an arbitrary safe point does not release a
  committed walk-back: it continues to `home_goal`, preventing frame-to-frame
  reversal. Immediately before `pad.stick()`, `guard_area_step()` projects the
  calibrated command by `AREA_LOOKAHEAD`; the entire proposed segment is checked,
  not just its endpoint, so it cannot cut through a concave notch, a gap between
  circle-union members, or an unsafe legacy-mask cell. `stick_for()` normalizes, so
  a redirected command is projected and checked again before dispatch; a nearby
  safe point alone is not proof that the full-strength heading is safe. A character
  already outside gets a measurable inward correction (with a lateral component
  for unwedge) rather than a zero stick. The corrected endpoint becomes the wall
  observer's goal even when loot originally owned the command. An unsafe step is
  blocked or redirected inward and attack is released. Calibration has no basis
  with which to project a direction yet, so
  an area run skips the unfenced wake nudge and calibration pushes only while the
  owned player has `AREA_SAFETY + AREA_LOOKAHEAD` of clearance; without a readable
  owner it fails closed rather than run the six-leg fallback across a boundary.
  Exception: startup/reconnect pixel fallback can already carry the player outside
  the area before calibration. Calibration must be allowed there, or the missing
  basis makes returning impossible and the bot remains on pixels forever; after
  the basis lands, the normal routed area return takes control immediately. A
  player still inside but too near the edge remains protected from probe pushes.
  **Explicit scanner-wait exception (approved 2026-08-26):** while memory reports
  `no unit` or `no monster`, temporary minimap targeting must still move and attack,
  including during `--area` runs. Before calibration supplies a basis this short
  fallback cannot enforce the world-coordinate fence; the user explicitly prefers
  immediate pixel actuation over holding still. Genuine stop modes and total memory
  construction failure remain neutral. Once basis and position exist, the final
  area guard applies to pixel vectors too.
- **A transient stable-ID miss is not a target switch.** FishNet `ObjectId` is
  preferred over a pointer, but one unreadable ID frame preserves the last verified
  ID and engagement clock. Only a positive different ID proves pooled-pointer reuse.
  The same stable identity keys held-target, engagement and ignore state. One
  hysteresis rule applies both inside and beyond `MEM_RANGE`: retain the held target
  unless the nearest fightable candidate is clearly nearer by `TARGET_SWITCH`.
- **Cell-size mismatch invalidates masks, never exact shapes.** Saving after a
  top-level `cell` change drops incompatible painted masks but preserves circles,
  circle unions, and polygons, which are already exact world coordinates.
- **The walk-back is routed, never steered straight.** The leash aimed raw at
  its anchor with `stick_for` and so leaned on rock for minutes; `_go_home()`
  goes through `route_to()`, and sits *below* the `unwedge` override so the
  physical escape always wins. When already inside the safe geometry, Dijkstra
  excludes out-of-area cells and boundary targets are projected to an inset-safe
  fighting point. The geometry itself triggers routing when a direct segment
  crosses a polygon notch, even without a learned wall, and diagonal steps cannot
  squeeze between two blocked corner cells. Return timeout also chooses `home()`,
  never the exact boundary. Once an exact-boundary target is reached at its nearest
  inset-safe fighting point, it enters `on it` and orbits inward-and-sideways;
  repeatedly issuing the blocked outward chase would release attack and deadlock.
- **A circle narrower than `AREA_SAFETY` has one honest safe point: its centre.**
  `home()` and wandering use a zero inner radius there rather than a half-radius
  point which the final movement guard would reject.
- **It returns the real distance, not zero.** `loot_wins()` compares that
  against the nearest drop, and a hard zero would make walking home unbeatable
  by an item two steps ahead of it -- the same trap `unwedge` sets.
- **`returning` is its own flag, not `self.mode`.** The leash derived it from
  the mode string, which is also the status line and is overwritten by half a
  dozen branches, so the state evaporated mid-return.
- **The area filter is applied *after* the sort, not inside it.** Filtering
  first meant `held` was looked up in the filtered list, so a monster stepping
  over the line vanished, `held` came back `None`, and the bot took a different
  target -- bypassing `TARGET_SWITCH` entirely, which is the only rule stopping
  it flapping between two monsters at a similar distance. `--targetlog` prints
  one line per target change and is how this was told apart from a single
  monster simply running away; both look identical on screen.
- **A wander point must be committed to, or the bot shakes on the spot.**
  `spot()` is uniform over the area, so on a small one most picks land inside
  `AREA_WANDER_REACHED` and count as *arrived the moment they are chosen* --
  the next frame picks another, 20 changes of direction a second. Reported as
  the bot "walking left and right continuously". Two things fix it and both are
  needed: `_wander_spot()` prefers a point at least `AREA_WANDER_MIN` away, and
  `AREA_WANDER_COMMIT_S` refuses to change target more often than that whatever
  happens -- an area too small to hold a distant point cannot flap either way.
  The demo asserts the *rate of change*, not the destination, because that is
  what the symptom actually was.
- **`spot()` samples each geometry correctly.** Masks pick uniformly from cells,
  circles use square-root radial sampling, and polygons reject samples from their
  bounding rectangle until one lies in the safe interior.
- **The cell you stand in is always painted**, whatever the brush: its centre
  can be further from you than `AREA_BRUSH`, and then a whole walk records
  nothing at all.
- **A recorder and the bot must not run at once.** The legacy recorder and bot
  both consume End; the new recorder uses independent edge-triggered configurable
  keys, but a moving bot still corrupts deliberate point placement.

### Loot pickup

- **`LootDrop` objects are pooled, so a position proves nothing -- the item's
  name is what says a drop is real.** The pool is a fixed set of objects that
  get recycled, and picking an item up frees neither the object nor its
  position, exactly the trap the pooled monsters set. No flag distinguishes
  them; that was ruled out by measurement, not assumed. `+0x22/+0x23` flip on
  most drops every few seconds (an animation/dirty bit), `InventoryItemData` and
  `LootSprite` are non-null on every slot including the dead ones, the native
  GameObject's other bytes are constant across all of them, and a pickup changes
  none of it. What a pooled slot does *not* have is an item: `loot_name()` reads
  empty for it. Measured on a live field, 157 of 192 slots were nameless and the
  35 with names were what was lying there; with the ground cleared, all 192 read
  empty. `world_loot()` therefore returns only named drops, and that single test
  is both the liveness check and the allowlist key.
- **The item name is in the synced payload, not on the drop.** `LootDrop`'s own
  `InventoryItemData` is zeroed on the client and there is no name within three
  hops of the object, which is what makes this look unavailable at first -- an
  earlier pass concluded exactly that and was wrong. It is at
  `LOOT_SYNC` -> `LOOT_NAME` (display name, as the tooltip shows) and
  `LOOT_KEY` (internal id): 'Flax'/'flax', 'Axe'/'T_Axe_Axe'. Confirmed against
  the screen -- 27 drops named Flax with Flax on the ground.
- **`LOOT_NAMES` entries are case-insensitive substrings.** `("Card",)` takes
  Bee Card, Rooster Card and any card added later; a full name still works.
  Requested deliberately over whole-name matching -- the item lists are families
  with a shared word, and writing every member out is what the list is meant to
  save. The cost is real: a short entry catches everything containing it, so
  "Axe" takes every Battle Axe too. Empty means take everything.
  `python memscan.py --loot` prints what is lying around, which is the list to
  write it from.
- **Who wins the frame is `loot_wins()`, and nearest-wins was not enough.** On a
  busy map a monster is almost always the nearer of the two, so drops a few steps
  away lost every arbitration until they despawned. Inside `LOOT_FIRST_RANGE` the
  item now takes precedence outright; beyond it, nearest still wins. Set
  `LOOT_FIRST_RANGE = 0` to restore the old rule.
- **Three modes are never interrupted, and all are load-bearing.** `on it` is a
  fight already joined — walking out of one is how a bot dies, and an item at our
  feet is collected by `LOOT_BUTTON` without moving anyway (`loot_here()`, checked
  even mid-fight, because the kill drops the item where we stand). `unwedge`
  reports a distance of **0.0**, so without excluding it every drop on the map
  looks nearer than the monster and the escape push would be overridden by loot
  — leaving the bot jammed against the wall it was in the middle of escaping.
  `going back` is committed confinement and likewise cannot lose to loot.
- **`LOOT_MAX_S` counts time spent walking to an item, never time it spent
  losing.** `loot_since` starts when a drop becomes the *candidate*, and the
  candidate is recomputed every frame whether or not loot won -- so an item that
  kept losing to a nearer monster was blacklisted for `LOOT_IGNORE_S` having
  never been approached at all. From outside that is a bot ignoring a drop at
  its feet, which is exactly how it was reported. `main()` restarts the clock on
  every frame the item loses. Issued-owner accounting also suspends monster time
  while loot owns the stick, and suspends both clocks during pause, return,
  unwedge, reconnect, wandering, or a boundary redirect.
- **Loot give-up follows one pooled-slot occupancy, not the wrapper pointer.**
  The spawn key combines wrapper, name and position. Reuse at a new position or
  with a new name starts a fresh clock and does not inherit the prior blacklist.
- **Why a drop was skipped has three causes that look identical**: not in the
  sweep's cache, blacklisted, or not in `LOOT_NAMES`. `loot_debug()` names which,
  and `--lootlog` prints it. Reach for that before theorising -- the filter was
  blamed first and the clock turned out to be the fault.
- **Loot needs its own give-up.** Same lesson as the monsters: an item that
  cannot be collected holds the bot on the spot pressing a trigger forever.
  `LOOT_MAX_S` then `LOOT_IGNORE_S`.

### Surviving a game patch

- **`LootDrop` has no `TYPE_RVA` entry and is not meant to have one.** It is
  found by name on the background thread (`MemoryEyes._ensure_classes`) and cached
  as an RVA like the rest. `heal()` only gates on the *monster* class, so missing
  loot/player classes recover independently. All missing optional classes share
  one streaming heap pass: separate player and loot passes exceeded five minutes
  after a patch while combat memory was already usable. A failed lookup retries
  on the throttled self-heal interval measured from scan completion; measuring
  from scan start can make a scan longer than the interval restart immediately.
- **`TYPE_RVA` is the only thing that breaks, and it breaks every patch.** Those
  are positions inside `GameAssembly.dll`; the 2026-08-11 update moved all three.
  The *field* offsets (position, health, visible, summoner) did not move and
  generally do not, because they follow the class layout, not the binary.
- **Class pointers are verified by name, never trusted.** `type_classes()` checks
  each slot against `CLASS_NAMES`. A stale RVA points at whatever moved in, and
  before this check that surfaced as invented units rather than as "the offsets
  are stale" — a wrong answer instead of an error.
- **`find_classes()` is the recovery, and it must stay streaming and off the hot
  path.** It searches memory for the class names and takes the object pointing at
  one; reflection data carries the same names, so instance count disambiguates
  (683 real against 0 for the impostors). It reads ~11 GB — an earlier version
  collected that into a list and never finished. Results are written back as
  fresh RVAs (`il2cpp_rva.json`, gitignored), making it once per patch, and it
  runs on the background thread while the bot works on pixels.
- **Scanner side effects belong to the generation that produced them.** Cache and
  dashboard publication checks are not enough: the scanner's closure-local
  previous-position map resets when `generation` changes, and the final generation
  check plus `WalkMap.paint()` run under the same lock. Otherwise a relog between
  publication and painting can turn reused object addresses into fictitious floor.

### Per-frame cost

- **`target()` runs at 20 Hz — do not read the whole unit list in it.** It was
  16.2 ms of a 50 ms budget with a 77 ms spike. Four things keep it at 2-4 ms and
  all of them matter: positions for monsters only (players and pets were a
  quarter of the work for something never targeted), units beyond `NEAR_KEEP`
  refreshed a slice per frame rather than all at once (that spike), liveness
  checked in distance order and cached for `LIVE_TTL_S`, and the position read
  plus its sanity check inline. Profile before changing any of it — the cost was
  Python overhead, not syscalls, which is not what it looks like.

### Rejected features — do not rebuild without new information

- **The anchor/leash/patrol feature was removed as buggy.** Double-tap End set an
  anchor, the bot stayed within a radius and walked home if it drifted out. It is
  in git history. Note that its worst symptom — ending 98 units outside a 77-unit
  leash — was not a leash bug at all but the calibration locking onto another
  player, so the idea is not disproven, only the implementation.

### Gamepad and vision

- **The game only reads XInput, not generic HID.** The Arduino path works over
  serial and the board enumerates fine, but the game ignores it. vgamepad is the
  working path; the Arduino backend is kept as a fallback, not the default.
- **The game sits in keyboard mode until it sees stick motion**, and it *drops the
  first button press* during the mode swap. Every button sequence must be preceded
  by `wake_controller(pad)` (stick nudge + `WAKE_SETTLE_S`). Removing it silently
  eats the leading d-pad press.
- **Red blobs within `CONCEAL_PX` of the box centre are never targets** — that is
  either "arrived" or a fixed red UI element. `TARGET_ARRIVE_PX` must stay above
  `CONCEAL_PX`.
- **The player's own pet cannot be excluded from the screen, and three attempts
  proved it — this is what memory targeting exists to solve, and it does.** On
  the memory path a pet is a pet (`SUMMONING_SUMMONER` is non-null) and is never
  a target. The constraint below still governs the pixel fallback.
- **(pixel path) The player's own pet cannot be excluded from the screen.** It
  renders identically to a monster; "follows the character" describes any aggro'd
  monster equally (measured: followers were monsters at 19–31px, the pet's own
  range); and a wider `CONCEAL_PX` fails because the pet wanders off to loot. Do
  not attempt a fourth screen-based fix: the identity of the dot has to come from
  somewhere other than the picture, which is exactly what memory targeting is.
- **Do not reintroduce marker detection *for our own character*.** Finding it as
  the nearest white blob failed two ways: the marker turns blue in a party, and in
  a crowd the nearest white blob belongs to another player. The centre is simpler
  and strictly more reliable. `find_white_players()` is the opposite job and is
  fine — it looks for *other* players to pair pets with, and excludes anything
  within `CONCEAL_PX` of the centre precisely so our own marker cannot qualify.
- **The bot never stands still waiting for a kill to finish, and cannot be made
  to.** Standing on a monster hides its dot inside `CONCEAL_PX`, which is
  indistinguishable from the monster dying — *our own pet follows the character
  and sits in that radius permanently*, so every "is something still under me"
  test answers yes forever. Two attempts failed here: a fixed 0.5s timer, then
  keeping the concealed dots and checking them (which parked the bot for a full
  6s after every kill, the pet being all it ever found). Attack is held
  continuously anyway, so walking straight to the next target keeps hitting.
  `TARGET_FLICKER_FRAMES` covers point-blank occlusion and nothing more — total
  gap after a kill is 0.15s. Do not reintroduce an engagement state without
  first solving own-pet identification.
- **The white player dot only renders in about half the frames.** Anything keyed
  on it needs memory across frames, which is why `PetFilter` confirms a pair and
  then tracks the pet by its own red marker. Do not "simplify" it to a plain
  distance test — the pet reappears as a target every other frame if you do.
- **The server row is found by template, never by position.** The list is sorted
  by ping and reorders between sessions, so a fixed row fraction picks a different
  region each time. `find_sea_row()` matches `sea_row.png` (the label text, cut at
  1920 wide and rescaled to the live width). No match means no click at all —
  never fall back to a fixed row. Buttons likewise match on width as well as
  position: Connect and Play Character are 0.033 apart vertically and were
  mistaken for each other on a live disconnect.
- **Login-screen detection needs a button *and* a backdrop, never one pixel.**
  `login_screen()` gates each screen on a blue button near a known fraction plus a
  probe of something only that screen has behind it. Gameplay has blue sky where
  the Ok button goes and blue skill icons where Play Character goes, so a lone
  colour probe fires mid-fight — and the consequence is a mouse click during
  combat. Keep both halves of every test. `RECONNECT = False` disables all of it.
- **Backdrop probes must sit at the edges, never where the character stands.**
  The character screen was never detected at all: `CHAR_BG` probed (0.30, 0.50),
  which is the middle of the screen, and a Weaver holding a lit cyan axe read
  255 there. Measured live -- `find_blue_button(PLAY_BTN)` returned the button at
  its nominal spot the whole time, so this looked like a missed click and was
  not one: no click was ever sent. The model, its pet and its weapon own the
  middle; probe (0.03, 0.50) and the other three edges instead.
- **Both halves still are not enough, and the reconnect flow must be able to give
  up.** Measured on a live session: `login_screen()` returned `"disconnected"`
  during ordinary play for the rest of the run. Nothing was on screen, and the
  status line read `memory` throughout, which only prints while our own unit is
  alive -- the client was never disconnected. The bot clicked `OK_BTN`
  (0.500, 0.144) into the world every `RECONNECT_POLL_S`, dropped the stick and
  `continue`d, so it stood still for hours and looked like a frozen End key.
  `RECONNECT_MAX_REPEAT` identical screens now turns reconnect off for the run,
  and the first `RECONNECT_DUMP_MAX` triggering frames are written to
  `reconnect_<screen>_<n>.png` -- which blob matched is the one thing the log
  cannot say, and without it the test can only be tightened by guesswork.
- **"No monster" is the unit list speaking, not the screen, so it must not zero
  the stick.** `main()` treats a zero stick as handled and never falls through to
  pixels; `hold_still()` holds that distinction and is asserted in `demo()`. Death
  and a rebuilt unit do stop the bot; an empty unit list hands over to the dots.
- **A dead scanner thread is invisible.** One raised read inside the sweep used to
  end the thread for good: the unit list froze, every frame reported `no monster`,
  and the End toggle would not restart it, because the guard tested
  `scanner is None` and a dead `Thread` is not None. The sweep now retries from
  the top, and the toggle checks `is_alive()`.
- **`START_PAUSED` must stay true.** Launching the script has to be safe; the bot
  waits for `End` (`TOGGLE_VK`) before it touches the stick.
- `ArduinoPad.stick` deduplicates against `self.last` because the sketch is
  synchronous (every command blocks on an `OK` reply). Keep it.

## Serial protocol (`arduino_joystick_leonardo_v1.ino`)

Line-oriented, one command per line, every command answers `OK` or `ERROR:...`.
`L<x>,<y>` left stick (int16), `R` right stick, `T` triggers, `V<0-7|-1>` hat
(clockwise from N, -1 centres), `B`/`D`/`U`/`H` button tap/down/up/hold, `G` full
state, `Z` release all, `P` → `PONG`. The board prints `READY` on boot; opening the
port resets it, so a fresh connection waits for `READY` and falls back to a `P`
ping if the banner was already missed.

## Other files

`memscan.py` is the memory layer — see Architecture. Its own `--demo` is a separate
self-check and must also stay green.

`minimap_navigator.py` is an earlier, superseded navigation experiment (argparse,
pywin32, proportional stick) kept for reference — new work goes in `minimap_bot.py`.

`il2cpp_rva.json` is a generated cache of rediscovered class slots. Gitignored, and
safe to delete — the bot searches again and rewrites it.

`walkmap.json` is the learned wall map. Gitignored, and safe to delete — the bot
relearns it, worse for a while. Delete it after a map change that moves geometry.

## Style

The codebase follows ponytail conventions: shortest thing that works, stdlib and
already-installed deps before new ones, and `# ponytail:` comments marking
deliberate simplifications and their upgrade path. Comments explain *why* a
non-obvious constant or branch exists (usually a game quirk), not what the code does.
