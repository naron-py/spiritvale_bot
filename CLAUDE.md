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
is read-only (`ReadProcessMemory`); nothing is written and nothing is injected.

## Commands

```
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

python minimap_bot.py              # run, vgamepad (virtual X360) backend
python minimap_bot.py --port auto  # run, Arduino Leonardo serial backend
python minimap_bot.py --demo       # the test suite (see below)
python minimap_bot.py --snap       # dump minimap_snap.png with detections drawn
python minimap_bot.py --test       # walk a blind circle: isolates pad vs vision
python minimap_bot.py --buff [hold] [gap]   # fire buff sequence once
python minimap_bot.py --probe      # press every X360 button in turn, named

python memscan.py --demo           # memory layer self-check, no game needed
python memscan.py --units          # list what the unit sweep classifies right now
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
   prints: `chasing`, `far`, `on it`, `gave up`, `no monster`, `lost`, `no unit`,
   `DEAD`. Everything it depends on is checked rather than trusted — see the
   constraints below.
4. **Pad backends** — `VirtualPad` (vgamepad/ViGEmBus, XInput) and `ArduinoPad`
   (serial to a Leonardo). Duck-typed, same three methods: `stick(sx, sy, attack)`,
   `tap_dpad(name, hold)`, `close()`. Pick a backend by adding a class with those
   three methods; nothing else in the file knows the difference.

`memscan.py` is the memory layer: region enumeration, the heap sweep
(`world_units`), the IL2CPP object walk, and class resolution. Field offsets and
`TYPE_RVA` sit in one block at its top. It is read-only by construction — it opens
the process with `PROCESS_VM_READ` and never calls `WriteProcessMemory`.

Tuning constants all sit in one block at the top of `minimap_bot.py`. Prefer
adjusting them over adding code paths.

## Hard-won constraints — do not "simplify" these away

### Memory targeting

- **Most of the unit list is not there to be fought.** Pooled and despawned
  monsters keep their last position *and get their health reset to full*, so they
  are indistinguishable from a healthy monster standing still. Measured: 516
  monster entries, 468 whose position had not changed in 2 seconds. Without
  `worth_fighting()` the bot parks in a pile of them, fights each for
  `MEM_ENGAGE_MAX_S`, gives up, takes the next from the same pile — and walks
  straight back if you drag the character away. The test is *rendered*
  (`IsVisible`) **and** health above zero; neither alone is enough.
- **Our own unit is identified by fit, never by who moved furthest.** On a busy
  map another player simply out-walks a 0.7s push, so the biggest-mover rule
  locked onto them and then threw every later leg away as "not us", failing
  calibration outright with a healthy character standing there. `pick_me()`
  scores each unit by how well one 2x2 basis explains all its legs: our travel is
  a linear function of the stick, a passer-by's is not, whatever their speed.
- **A dead character deals no damage and looks exactly like a targeting bug.**
  It reads as "melee range must be wrong" and sent a whole session chasing a
  `MEM_ARRIVE` change the evidence did not support. `target()` reports `DEAD`
  instead of swinging from a corpse. `MEM_ARRIVE = 2.5` is correct — verified
  killing at that distance on a living character.
- **A far target keeps the engagement clock.** Clearing it each frame meant the
  give-up timer never fired and an unreachable monster could be walked at forever.
- **Never return a zero stick for "nothing to do".** `main()` treats a zero stick
  as handled and does not fall through to the pixel path, so the bot stands
  still. That is why no monster within `MEM_RANGE` walks to the nearest one
  anywhere (`far`) rather than returning nothing.
- **One empty position read is not a death.** Tearing down `me`, `basis` and the
  caches on the first miss cost a whole run — the bot went silent until it was
  restarted. `MEM_LOST_FRAMES` consecutive misses are required.
- **Every mode assignment must be honest, including the early returns.** Leaving
  a stale `mode` made a bot with no unit at all report `chasing` while motionless,
  and sent the investigation to the wrong place.

### Surviving a game patch

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

## Style

The codebase follows ponytail conventions: shortest thing that works, stdlib and
already-installed deps before new ones, and `# ponytail:` comments marking
deliberate simplifications and their upgrade path. Comments explain *why* a
non-obvious constant or branch exists (usually a game quirk), not what the code does.
