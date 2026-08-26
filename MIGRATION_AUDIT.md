# SpiritVale Bot — Python → C#/BepInEx Migration Audit (Phase 0)

Read-only audit of the existing repository and the installed game. No code was
modified. Every game-tech fact below was verified against the files on disk, not
assumed.

---

## 0. Executive summary

- The existing bot is **already game-state-driven**, not screen-driven. Its
  primary path reads the game's unit list from process memory (read-only,
  out-of-process). The screen/OpenCV path is a **fallback**, not the main loop.
  This changes the shape of the "migration" substantially (see §3).
- The game is **Unity 6 (6000.0.64f1), IL2CPP, FishNet networking, no
  client-side anti-cheat** found. BepInEx 5 (Mono) is not applicable; only the
  **BepInEx 6 + Il2CppInterop** path is viable, and its compatibility with this
  exact Unity patch must be proven empirically (Phase 1).
- The IL2CPP metadata **retains human-readable class and field names**
  (the Python bot already resolves `MonsterController`, `PlayerController`,
  `HealthComponent`, etc. by name). This is good news: Il2CppDumper will
  generate C# bindings with real names, so the hardcoded-offset approach in
  `memscan.py` can be replaced by named field access.
- **Several features in the migration brief do not exist in the current bot**
  (inventory, weight, town, storage, sell/repair). Those are greenfield, not
  migrations. See §5.
- **Blocking decision:** this migration reverses a constraint the project and
  the user previously set (read-only, no injection, no BepInEx). It must be
  confirmed before any in-process C# is written. See §7.

---

## 1. Game technology (verified against the install)

Install path: `D:\Games\Steam\steamapps\common\SpiritVale`

| Fact | Value | Evidence |
|---|---|---|
| Engine | Unity **6000.0.64f1** (Unity 6) | `UnityPlayer.dll` version resource |
| Backend | **IL2CPP** (not Mono) | No `SpiritVale_Data/Managed/`; `GameAssembly.dll` (107 MB) present; `il2cpp_data/Metadata/global-metadata.dat` (23.7 MB) present |
| IL2CPP metadata version | `0xFAB11BAF` | first 4 bytes of `global-metadata.dat` |
| Networking | **FishNet** | `FishNet.SDK.Id`, `client_assets_fishpink_*.bundle`, FishNet classes in metadata |
| Publisher | **Baikun** | `SpiritVale_Data/app.info` |
| Anti-cheat | **None found** | No EAC / BattlEye / kernel driver / protection DLL. `abci.dll` is **Alembic** (open-source 3D scene library — `Ogawa@Alembic` mangled symbols), not AC. Only Steam + `UnityCrashHandler64.exe`. |
| Renderer | D3D12 | `D3D12/` folder |
| Toolchain on host | .NET SDK **9.0.313** | `dotnet --version`. No BepInEx or Il2CppDumper present yet. |

Implications:
- **BepInEx 5 is out** (Mono-only). The only in-process path is **BepInEx 6 +
  Il2CppInterop** (generates C# bindings from IL2CPP metadata).
- Unity 6 (6000.x) is new; BepInEx 6 tracks it, but the fit with this specific
  patch (6000.0.64f1) is **not confirmed here** and is the #1 technical risk.
- No client-side AC means no *local* integrity checks to dodge, but this is a
  **ToS violation** and the game could add server-side or client-side checks at
  any patch. "No AC found today" is not a guarantee.

### 1a. IL2CPP metadata verified (Il2CppDumper v6.7.46, run against this install)

The dump (`dump.cs`, 54 MB) confirms the metadata **retains human-readable class
and field names**, and that **every hardcoded offset in the Python bot's memory
layer matches the real IL2CPP layout exactly**. This is the load-bearing fact for
the whole migration: the C# port can use *named* fields instead of magic offsets,
and the existing offsets are already correct.

| Python bot reads (offset) | IL2CPP dump (class.field @ offset) | Match |
|---|---|---|
| `UNIT_POSITION 0x190` | `BaseUnitController._lastValidPosition` `Vector3` @ 0x190 | ✓ |
| `UNIT_HEALTH 0x128` | `BaseUnitController.<Health>k__BackingField` `HealthComponent` @ 0x128 | ✓ |
| `UNIT_VISIBLE 0x18D` | `BaseUnitController.IsVisible` `bool` @ 0x18D | ✓ |
| `MONSTER_ID 0x218` | `MonsterController.MonsterId` `string` @ 0x218 | ✓ |
| `STATUS_FLAGS 0x188` | `StatusComponent.<Flags>k__BackingField` `SyncVar<uint>` @ 0x188 | ✓ |
| `HEALTH_CURRENT 0x138` | `HealthComponent._health` `int` @ 0x138 | ✓ |
| `LOOT_ITEM 0x100` | `LootDrop.ItemData` `InventoryItemData` @ 0x100 | ✓ |
| `LOOT_NAME 0x108` | `LootDrop.Dto` `SyncVar<LootDropDto>` @ 0x108 | ✓ |
| `LOOT_KEY 0x110` | `LootDrop.RarityEffect` `GameObject` @ 0x110 | ✓ (see note) |

All key classes resolve with real names: `MonsterController : BaseUnitController`,
`PlayerController : BaseUnitController`, `BaseUnitController : NetworkBehaviour`,
`HealthComponent`, `LootDrop : NetworkBehaviour, IInteractable`,
`SummoningComponent`, `StatusComponent`, plus the FishNet chain
`TransportManager → NetworkManager → ClientManager → NetworkObject →
NetworkConnection → NetworkBehaviour`.

**New capabilities the dump reveals (beyond what the Python bot reads):**
- **Boss/elite detection** — `MonsterController.MonsterRank` is an enum:
  `Normal=0, MiniBoss=1, Boss=2, Pet=3`. The brief's boss/elite priority is
  directly supported.
- **Inventory is readable, not greenfield** — `CharacterData.Inventory`
  (`InventoryData`) is a set of categorized dictionaries: `Equips`, `Artifacts`,
  `Cards`, `Gems`, **`Junks`**, `Consumables`, `Cosmetics`. The game already
  classifies junk. `InventoryItemData` has `Id`, `Favorite`; `StackableItemData`
  adds `Count`.
- **Weight is readable** — `GetWeightLimit(PlayerController)` method,
  `StatType.WeightLimit = 101`, and `get_Weight()` accessors exist.
- **NavMesh is present** — `UnityEngine.AI.NavMeshBuilder`, `NavMeshAgent` are in
  the dump, so the in-process path can call the real navmesh (replacing the
  learned `WalkMap`).

`LOOT_KEY 0x110` note: the Python bot reads a "key" there, but the dump shows
`RarityEffect` (a GameObject) at 0x110 and the item identity actually lives in
`ItemData` (0x100) / `Dto` (0x108). The C# port should read `ItemData`/`Dto`
directly and drop the 0x110 guess — a correctness fix, not just a rename.

---

## 2. Repository inventory

| File | Lines | Role |
|---|---|---|
| `minimap_bot.py` | 5365 | The bot: vision, control, memory targeting, walk-map, areas, pad backends, main loop, dashboard, reconnect, self-test |
| `memscan.py` | 2292 | The memory layer: process open, region enumeration, heap sweep, IL2CPP object walk, class resolution, loot read |
| `minimap_navigator.py` | ~400 | Superseded navigation experiment (argparse, pywin32, proportional stick). **Do not port.** |
| `arduino_joystick_leonardo_v1.ino` | — | Arduino Leonardo HID sketch (serial pad backend) |
| `requirements.txt` | 6 | `mss`, `opencv-python`, `numpy`, `pygetwindow`, `vgamepad`, `pyserial` |
| `il2cpp_rva.json` | — | Gitignored cache of rediscovered class slots (safe to delete) |
| `walkmap.json` | — | Gitignored learned wall map (safe to delete) |
| `areas.json` | — | Gitignored recorded farming areas |
| `loot_names.txt` | — | User-editable wanted-item substrings |
| `CLAUDE.md` | — | Project constraints (read-only, no injection) |

Branch: `read-memory` (remote `naron/main`, `origin/read-memory`).

---

## 3. Current architecture (what the bot actually does)

Two targeting sources, memory primary, pixels fallback:

```
                    ┌────────────────────────────────────────────┐
                    │                 main()  20 Hz loop          │
                    │  End toggle · buff timer · loot tap · spam  │
                    └───────────────┬────────────────────────────┘
                                    │
            ┌───────────────────────┴───────────────────────┐
            │                                               │
   MEMORY PATH (primary)                          PIXEL PATH (fallback)
   MemoryEyes + memscan.py                        find_red_dots + OpenCV
            │                                               │
   ReadProcessMemory (read-only)              mss grab of minimap box
   background heap sweep (2 s)                HSV threshold + contours
   unit list: kind, addr, x,y,z              PetFilter / TargetLock /
   local_player pointer walk                  StuckWatchdog / Blacklist
            │                                               │
   calibrate (stick→world basis)              stick_vector (direction-only)
   target(): chase / far / on it / orbit
   pick_loot() · route_to() · WalkMap · Area
            │                                               │
            └───────────────────────┬───────────────────────┘
                                    │  (sx, sy, attack)
                                    ▼
                        Pad backend (VirtualPad / ArduinoPad)
                        vgamepad (XInput) or serial Leonardo
```

### 3.1 Memory path (the real workhorse)

`memscan.py` (out-of-process, read-only):
- `Mem` — `OpenProcess(PROCESS_VM_READ)`, `ReadProcessMemory`,
  `VirtualQueryEx64` region enumeration. **Never writes.**
- `type_classes` / `find_classes` / `heal` — resolve IL2CPP class slots from
  `TYPE_RVA` (monster / player / summoning), or **search by class name**
  (`MonsterController`, `PlayerController`, `SummoningComponent`, `LootDrop`)
  when a patch moves the RVAs. Results cached to `il2cpp_rva.json`.
- `world_units` / `instances_of` — heap sweep to find every instance of a
  class. First pass reads ~8 GB (~14 s); narrowed to "hot" regions afterwards
  (~1 s). This is the most fragile, most-tuned part of the bot (the entire
  `hot` / `hot_loot` / re-narrow / self-heal / empty-streak machinery exists to
  keep a narrowed sweep from going stale).
- `local_player` — pointer walk from any unit:
  `unit → TransportManager(0x28) → NetworkManager(0x68) → ClientManager(0x38)
  → Connection(0x60) → NetworkObject(0x68) → NetworkBehaviours(0x60) →
  PlayerController`. This is how "who is us" is read, not walked for.
- `world_loot` — find `LootDrop` instances, read item name / internal id /
  position through the drop's SyncVar payload.
- Field offsets (one block at top): `UNIT_POSITION 0x190`, `UNIT_HEALTH 0x128`,
  `UNIT_VISIBLE 0x18D`, `MONSTER_ID 0x218`, `STATUS_FLAGS 0x188`,
  `SYNCVAR_VALUE 0x74`, `STATUS_INVISIBLE 0x20`, `HEALTH_CURRENT 0x138`,
  `LOOT_ITEM 0x100`, `LOOT_NAME 0x108`, `LOOT_KEY 0x110`, plus the FishNet
  pointer-chain offsets.
- `real_monster` / `worth_fighting` / `monster_target_state` — liveness:
  rendered (`IsVisible`) **and** health > 0 **and** has `MonsterId` **and** not
  cloaked (`Status.Flags.Invisible`). Each conjunct was earned by a measured
  failure (see CLAUDE.md).

`minimap_bot.py` `MemoryEyes` (the targeting engine):
- `calibrate(pad)` — learn the 2×2 `basis` (stick push → world travel) by
  pushing the stick and measuring our own unit's displacement. Two legs if the
  owner is known, six otherwise (`pick_me` fallback scores units by how well one
  basis explains all their legs — never "who moved furthest").
- `target(now)` — returns a stick vector + a `mode` string: `chasing`, `far`,
  `on it`, `gave up`, `no monster`, `lost`, `no unit`, `invisible`, `walled`,
  `wander`, `going back`, `unwedge`. Holds the current target (no per-frame
  re-pick), orbits at `MEM_ORBIT_MIN..MAX` standoff, gives up after
  `MEM_ENGAGE_MAX_S`, blacklists unreachable/walled targets.
- `pick_loot(now)` — nearest wanted drop within `LOOT_RANGE`; arbitration vs.
  the monster path (`loot_wins`); `LOOT_BUTTON` (left trigger) tapped on arrival.
- `route_to` / `observe_move` / `wedge_off` — feed and consume the `WalkMap`.
- `_wander` / `_go_home` — recorded-area idle patrol and walk-back.
- `start_scanning` — the background sweep thread (units + loot + walk-map floor
  paint + save), with a generation counter to invalidate sweeps across a relog.

### 3.2 Walk-map (learned navigation)

`WalkMap` — a coarse grid over world (x, z), three states (blocked / floor /
never-seen; never-seen routes as passable). Walls learned from **our own**
movement (two sensors: fast "did I travel the way I pushed" + slow "am I getting
closer to the goal"), floor learned from **other units'** movement (they walk the
same navmesh). Dijkstra on an 8-connected grid, corridor-bounded, weighted to
prefer proven floor. Persisted to `walkmap.json`. This exists because the game's
walkability is a **Unity NavMesh** whose polygons are native Detour structures —
the Python bot deliberately does *not* call into them (would end the read-only
guarantee). **In-process, this becomes replaceable by the real NavMesh** (see §5).

### 3.3 Pixel path (fallback only)

`find_red_dots` (HSV + contour centroids over an `mss` grab of the minimap box),
`find_white_players`, `PetFilter` (pairs a pet's red marker with its owner's
white marker, tracks it across frames), `TargetLock`, `StuckWatchdog`,
`TargetBlacklist`. Runs during the first background sweep and whenever memory
targeting is unavailable. **Most of this disappears in-process**, because memory
already knows what each thing *is*.

### 3.4 Reconnect / login

`login_screen` / `reconnect_step` / `find_sea_row` / `find_blue_button` —
template-match + mouse-click automation for the disconnect → server → character
flow. Gated on a button **and** a backdrop (never one pixel), with a
`RECONNECT_MAX_REPEAT` give-up. This is the one screen-based feature with no
memory equivalent today.

### 3.5 Input

Two duck-typed pad backends, same methods (`stick`, `tap_dpad`, `tap_button`,
`tap_trigger`, `close`): `VirtualPad` (vgamepad / ViGEmBus, XInput) and
`ArduinoPad` (serial Leonardo). The game reads XInput; vgamepad is the live path.

### 3.6 Configuration / logging / timing

- Config: one big constants block at the top of `minimap_bot.py` (tuning knobs)
  + `loot_names.txt` (user-editable) + `areas.json` / `walkmap.json` /
  `il2cpp_rva.json` (gitignored state). No structured config file.
- Logging: `print()` with `--fightlog`, `--walklog`, `--lootlog`, `--targetlog`,
  `--lootlog` flags + a `TerminalDashboard` (4 Hz redraw). No structured logger.
- Timing: fixed 20 Hz loop (`time.sleep(1/LOOP_HZ)`); background sweep on a
  2 s timer; dashboard at 4 Hz.

---

## 4. Migration matrix

| Python (file:component) | Responsibility | C# / BepInEx destination | Difficulty | Status |
|---|---|---|---|---|
| `memscan.Mem` | Open process, `ReadProcessMemory`, region enum | **Eliminated** — in-process, direct object refs | — | Replaced |
| `memscan.type_classes` / `find_classes` / `heal` | Resolve IL2CPP class slots by RVA or name search | **Il2CppInterop generated bindings** (Il2CppDumper) | Low | Auto per patch |
| `memscan.world_units` / `instances_of` | Heap sweep to find all unit instances | **Direct enumeration** via FishNet `NetworkManager` / generated bindings | Med | Phase 2 |
| `memscan.local_player` | Pointer walk to the local `PlayerController` | **Direct C#** via bindings (a few lines) | Low | Phase 2 |
| `memscan.world_loot` + loot offsets | Find `LootDrop`, read name/id/pos | **Direct C#** via bindings | Low | Phase 2 |
| `memscan.real_monster` / `worth_fighting` / `monster_target_state` | Monster liveness (rendered + hp + id + not cloaked) | **Direct C#** field reads | Low | Phase 3 |
| `minimap_bot.MemoryEyes` | The whole targeting engine (calibrate, target, orbit, loot, wander, area, routing) | **C# TargetManager + Combat + Loot + Navigation** | **High** | Phases 3–6 |
| `minimap_bot.WalkMap` | Learned wall/floor grid + Dijkstra | **Port** — or **replace with Unity NavMesh** (in-process) | Med | Phase 6 |
| `minimap_bot.Area` | Recorded farming fence | **C# Area** (port) | Low | Phase 6 |
| `minimap_bot.VirtualPad` / `ArduinoPad` | Gamepad output | **Keep as-is** (out-of-proc pad) or in-process input | Low | Optional |
| `minimap_bot` pixel path (`find_red_dots`, `PetFilter`, `TargetLock`, `StuckWatchdog`, `TargetBlacklist`) | Screen fallback | **Mostly eliminated** — memory knows what things are; keep only if needed | Low | Phase 10 (remove) |
| `minimap_bot.login_screen` / `reconnect_step` / `find_sea_row` | Reconnect UI automation | **Keep** (mouse click still needed) or read UI state in-proc | Med | Phase 9 |
| `minimap_bot.main()` | 20 Hz loop, state, dashboard, buff/loot/spam timers | **C# BotCore** (MonoBehaviour / coroutine) | Med | Phase 5+ |
| `minimap_bot.demo()` | Assert self-test on synthetic data | **C# unit tests** (xUnit/NUnit) + mock game state | Med | Per phase |
| `minimap_navigator.py` | Superseded experiment | **Do not port** | — | Skip |

---

## 5. What changes, what survives, what's new

### Replaced by direct game-state access (the win)
- **All ctypes memory reads → direct C# field access.** Faster (no syscalls),
  and the hardcoded offsets become named fields from the generated bindings.
- **The 8 GB heap sweep disappears.** In-process, units are enumerated from
  FishNet's own structures / generated bindings. This deletes the entire
  `hot` / `hot_loot` / re-narrow / self-heal / empty-streak / generation-counter
  machinery — the most fragile, most-tuned code in the bot — along with the
  14 s first-sweep latency and the "stale cache" bug class.
- **Class RVA search + `il2cpp_rva.json` → Il2CppInterop bindings.** Regenerated
  per patch, but class *names* don't move, so it's far more robust than the
  Python bot's "search by name for 2–4 minutes" recovery.
- **`WalkMap` (learned walls) → Unity NavMesh.** In-process we can call
  `NavMesh.CalculatePath` / `NavMesh.SamplePosition` directly. The learned-grid
  fallback can be kept as a safety net, but the primary path becomes the game's
  own navmesh. (This is exactly the in-process capability the Python bot
  deliberately avoided.)
- **`local_player` pointer walk → a few lines of C#** using the bindings.

### Survives (still needed, ported)
- **Target selection logic** (hold target, orbit, give-up, blacklist, area
  confinement, loot arbitration) — this is pure game-state logic and ports
  cleanly.
- **The pad backends** — unless we drive the game's own input system in-process
  (a Phase 2 decision; the game reads XInput, so a virtual pad may still be the
  most reliable actuator).
- **Reconnect / login UI** — needs a mouse click; in-process we can *read* the
  UI state but still have to click.

### Net-new (NOT in the current bot — do not call these "migrations")
The brief references inventory, weight, town, storage, sell/repair as if they
exist. **They do not exist in the current Python bot** (verified: no
`inventory` / `weight` / `bag` / `storage` / `warehouse` / `sell` / `repair` /
`restock` / `town` / `teleport` logic in `minimap_bot.py` or `memscan.py`). So
the *bot code* for them is new work. **However, the IL2CPP dump (§1a) shows the
game already exposes the data**, so these are "read existing game state + write
new bot logic," not "reverse-engineer the data model from scratch":
- **Inventory / weight** (brief §24–25) — data is readable:
  `CharacterData.Inventory` (`InventoryData`, categorized dicts incl. `Junks`),
  `GetWeightLimit(PlayerController)`, `StatType.WeightLimit`, `get_Weight()`.
  The bot logic (thresholds, KEEP/STORE/SELL/DISCARD) is new.
- **Boss/elite priority** (brief §13) — `MonsterRank` enum
  (`Normal/MiniBoss/Boss/Pet`) is directly available; the priority table is new.
- **Town / storage / sell / repair / restock** (brief §26–28) — *data* may be
  exposed (storage helpers exist), but the *actions* (open storage, deposit,
  sell, buy) are UI-driven and are the genuinely greenfield, highest-uncertainty
  part. Phase 2 must confirm what is actually reachable in-process.
- **A real state machine** (brief §31) — the Python bot has *implicit* modes
  (`eyes.mode` strings), not a formal FSM. Building one is new work, though the
  existing mode strings map onto the suggested states.
- **"Pet collects items too slowly"** (brief §19) — the bot *already* picks up
  loot itself via `LOOT_BUTTON` (left trigger) when standing on a drop. This is
  a desired enhancement (more aggressive loot), not a current gap.

---

## 6. Proposed C# / BepInEx architecture

```
┌────────────────────────────────────────────────────────────┐
│ GAME (Unity 6, IL2CPP, FishNet)                            │
│   GameAssembly.dll + global-metadata.dat                   │
│        │  Il2CppDumper → generated C# bindings             │
│        ▼                                                   │
│   BepInEx 6 (Il2CppInterop)                                │
│        │                                                   │
│        ▼                                                   │
│   GameBridge  (thin, cached, no per-frame allocation)      │
│     • LocalPlayer  (PlayerController ref, transform)       │
│     • UnitSource   (FishNet NetworkManager → live units)   │
│     • LootSource   (LootDrop enumeration)                  │
│     • NavMesh      (CalculatePath / SamplePosition)        │
│     • Inventory    (weight / slots — Phase 2 discovery)    │
│        │                                                   │
│        ▼                                                   │
│   BotCore (MonoBehaviour, 20 Hz coroutine)                 │
│     • EntitySystem   (Self/OtherPlayer/Monster/Boss/Pet/   │
│                       Summon/Unknown — classified)         │
│     • TargetManager  (classify→ownership→hostility→        │
│                       visibility→reachability→priority)    │
│     • Combat         (orbit, engage clock, give-up)        │
│     • LootManager    (filter, priority, distance, arb.)    │
│     • Navigation     (GoTo / waypoint / NavMesh / WalkMap  │
│                       fallback / wedge escape)             │
│     • Area           (recorded fence, wander, walk-back)   │
│     • StateMachine   (IDLE/FARMING/COMBAT/LOOT/…/RECOVERY) │
│     • InventoryMgr   (weight thresholds, KEEP/STORE/SELL)  │
│     • TownManager    (return, storage, sell, restock)      │
│     • Input          (pad backend OR in-process input)     │
│     • Reconnect      (UI read + mouse click)               │
│        │                                                   │
│        ▼                                                   │
│   Config (BepInEx ConfigFile) · Logger (BepInEx + tags)    │
└────────────────────────────────────────────────────────────┘
```

Design rules carried over from the Python bot's hard-won constraints (these
transfer regardless of language):
- Never return a "zero / nothing" action that the main loop reads as "handled"
  and parks the bot — a far target must still produce motion.
- Liveness is conjunctive (rendered + hp + id + not cloaked); no single flag is
  sufficient.
- Target hold + give-up clock + blacklist; a target that won't die must be
  dropped.
- Orbit at standoff (a zero stick drops the game to keyboard mode and stops the
  attack landing; standing on the target gives no swing room).
- Walls: progress-not-speed, two sensors, fan not cell, wedge escape, floor from
  other units never erases a measured wall.
- One empty position read is not a death (require N consecutive misses).
- Every mode/state assignment must be honest, including early returns.

---

## 7. BLOCKING DECISION — constraint conflict

This migration **reverses a constraint the project and the user previously set**:

- **CLAUDE.md** (project hard constraint): the bot is "read-only by
  construction — it opens the process with `PROCESS_VM_READ` and never calls
  `WriteProcessMemory`," and explicitly rejects in-process approaches ("calling
  `CalculatePathInternal` in-process … would end this bot's read-only
  guarantee").
- **User's standing recorded preference**: "authorizes read-only external
  process-memory observation for game automation, but **prohibits BepInEx,
  injection, memory writes, hooks, and anti-cheat bypass**."

A BepInEx plugin is, by definition, in-process injection. Proceeding to Phase 1
means overriding both of the above.

Consequences of the override (even with no client-side AC found today):
- **ToS violation** — in-process modification is a terms-of-service breach for
  virtually all online games; risk is account/ban, not a local crash.
- **Future AC** — the game can add client- or server-side integrity checks at
  any patch; a BepInEx plugin is far more detectable than read-only memory
  reads.
- **Stability** — a bad plugin can crash the game process (worse than a Python
  bot that just stops).

This is a deliberate, high-stakes reversal, so it needs explicit confirmation
before any in-process C# is written. The Phase 0 audit above is complete and
valid for either path.

---

## 8. Phase 1 — BepInEx proof of concept (spec, gated on §7)

Smallest possible plugin, no bot logic:
1. Il2CppDumper against `GameAssembly.dll` + `global-metadata.dat` → C# class
   library. Confirm `MonsterController`, `PlayerController`,
   `BaseUnitController`, `HealthComponent`, `LootDrop` resolve with real names.
2. Minimal BepInEx 6 plugin (Unity 6 / .NET target matching the game) that:
   - logs on load,
   - finds the local `PlayerController` (via the FishNet pointer walk, now in
     C#),
   - logs its world position each second.
3. Verify: BepInEx loads, plugin runs, player identified, position readable.

Success = the in-process path is proven. Only then do Phases 2–10 proceed.

---

## 9. Risks / unknowns (cannot be confirmed from here)

1. **BepInEx 6 + Unity 6000.0.64f1 compatibility** — must be proven by actually
   loading a plugin. Highest technical risk. (Il2CppDumper v6.7.46 parses this
   game's metadata cleanly — metadata version 31 — so the *bindings* side is
   proven; the *loader* side is not.)
2. **Constraint override** (§7) — needs user confirmation.
3. **ToS / ban risk** — no client-side AC today, but that is not a guarantee.
4. **In-process unit enumeration** — how to get the live unit list cleanly
   (FishNet `NetworkManager` vs. generated-binding instance scan) is a Phase 2
   discovery, not yet known. The Python bot's 8 GB heap sweep is what this
   replaces; the C# equivalent must be found, not assumed.
5. **Input actuation** — whether to keep the virtual pad or drive the game's
   own input system in-process is a Phase 2 decision.
6. **Town / storage / sell / repair *actions*** — the data is readable (§1a),
   but the UI-driven actions (open storage, deposit, sell, buy) are the
   genuinely greenfield, highest-uncertainty part. Phase 2 must confirm what is
   actually reachable in-process before promising them.
7. **Patch churn** — every game patch moves RVAs; Il2CppDumper must be re-run.
   Class/field *names* are stable, but the dump is a per-patch step. (The
   Python bot's `il2cpp_rva.json` self-heal exists for exactly this; the C#
   equivalent is "re-dump + rebuild.")
8. **NavMesh access** — `UnityEngine.AI` is present in the dump, but the exact
   Unity 6 navmesh API surface (and whether the game populates it client-side)
   must be confirmed in-process before relying on it to replace `WalkMap`.
