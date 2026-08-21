# Changelog

## Branch `read-memory`

58 commits against `main`, plus the uncommitted work described at the end.

The branch does one big thing: it gives the bot a second way of seeing. Before,
the bot knew only what the minimap showed it — red dots, which cannot say
whether a dot is a monster, another player, or your own pet. Now it can read the
game's own unit list instead, and everything else here follows from that.

---

## Memory targeting

Reads the game's unit list through `ReadProcessMemory`. **Read-only by
construction**: the process is opened with `PROCESS_VM_READ` only, nothing is
written and nothing is injected.

- Targets monsters by what they *are*, not how they look. Your own pet, other
  players' pets and terrain art stop being targets — the problem three separate
  screen-based attempts failed to solve.
- Our own character is found by a pointer walk (unit → TransportManager →
  NetworkManager → ClientManager → the local connection), not by pushing the
  stick and seeing who moved. On a busy map another player out-walks a short
  push, and the old rule locked onto them.
- Calibration still walks, but only to learn the stick→world mapping, and takes
  two legs instead of six.
- Two filters that are not optional: a monster with no `MonsterId` cannot be
  damaged, and a pooled monster keeps its last position *and full health*, so
  both look perfectly alive. Without these the bot parks in a pile of corpses.
- Survives a game patch: class pointers are verified by name, and searched for
  again in the background when a patch moves them.
- Falls back to the minimap whenever memory targeting is unavailable.

## Loot pickup

- Walks to ground drops and collects them.
- A drop is real when it *has an item name* — the pooled `LootDrop` objects keep
  their position after pickup, so position proves nothing. The name lives in the
  synced payload, not on the drop itself.
- `LOOT_NAMES` entries are case-insensitive substrings, so `("Card",)` takes
  every card. Empty means take everything.
- Loot never interrupts a fight already joined, and an item at your feet is
  collected without moving.

## Walking round walls

- `WalkMap`: a learned grid of where the character can and cannot go, saved to
  `walkmap.json`.
- Walls come from our own failed movement; floor comes from every *other* unit's
  position, since they walk the same navmesh we do.
- Routes with a weighted search that prefers proven floor, bounded to a corridor
  around the straight line.
- A wedged character is freed with a sideways shove, alternating sides — a
  router cannot free something the physics has jammed.

## Diagnostics

`--walklog`, `--lootlog`, `--fightlog`, `--targetlog`, `--snap`, `--watch`,
`--test`, `--probe`, and `memscan.py --units` / `--loot` / `--check`.

---

## Uncommitted (this session)

### Fixed: fake walls from a slow position feed

The bot walked three steps, turned, walked three steps, turned — forever.

`UNIT_POSITION` is `_lastValidPosition`, which is server-validated rather than
the live transform, so it **repeats the same value for several frames at a
time as a matter of course**. The wall sensor counted a repeat as "I did not
move" and stamped a wall on open ground.

Measured at 20 Hz: walking by hand, 4% of frames repeated, longest run 8. With
the bot driving, runs of 3, 4, 5, 6, 7 in one 8-second window — and later 15, 17
and 26 with the feed down at 12.4 Hz.

- Neither sensor may judge on a repeated position. The slow one was fooled just
  as badly, since a stall spanning its window leaves both endpoints identical.
- Being jammed is measured as **displacement across a window** (`WALK_JAM_S`),
  not as a run of repeats. A stall ends with the position jumping to where the
  character actually walked; a jam ends where it started.
- A first attempt using a 1.0 s no-fresh-position timer is documented as
  **rejected** — the feed produced 1.3 s stalls under load and it fired on every
  one.

Four causes were measured and ruled out before this one: the read path is
healthy (36 units moved in the same window), `local_player()` has the right unit
(51.4 units of travel while walking), holding attack does **not** root the
character (it is the smoother case), and a zero stick does **not** strand the
game in keyboard mode.

### New: named farming areas

Walk the ground you want farmed, save it under a name, and the bot stays there.

```
python minimap_bot.py --record lunaris     # walk it; End saves
python minimap_bot.py --area lunaris       # run confined to it
```

- The area is the cells you walked, widened by ~6 units either side, saved to
  `areas.json`. Recording an existing name adds to it.
- Monsters and loot outside the area are never targeted; the bot walks back in
  if it ends up outside, routed through the wall map rather than in a straight
  line; and it wanders inside the area when nothing is left to kill.
- `AREA_SLACK` < `AREA_HOLD` < `AREA_LEAVE` are one ordered chain, because with
  a single number the bot chased a legal target just past the line, reaching it
  put the character "outside", and it walked home — then chased the same legal
  target again, alternating `dist` and `back in` forever.
- The area filter is applied *after* the sort, so the held target is still found
  — filtering first bypassed `TARGET_SWITCH`, the rule that stops the bot
  flapping between two monsters.
- Wander commits to a point for at least 1.5 s. Without that, a random point
  closer than "arrived" counted as reached the instant it was chosen, and the
  bot changed direction 20 times a second.
- **Nothing in the game identifies which map you are on** — no scene name, no
  zone id, anywhere in the code or the memory layer. So areas are named by hand,
  and `--area` on the wrong map is caught by distance and switches the
  confinement off with a printed reason rather than walking into scenery.

### New: choose the target source

```
python minimap_bot.py             # minimap, red dots (default)
python minimap_bot.py --memory    # the game's unit list
python minimap_bot.py --minimap   # force the screen path
```

Minimap is the default: it needs nothing from the game's memory, starts
instantly and survives a patch. `--area` implies `--memory`, and an explicit
`--minimap` always wins — the bot says the area is being ignored rather than
silently overriding a flag you typed.

### Removed

The anchor/leash/patrol feature was rejected as buggy earlier on this branch
(431 lines). Its lessons are recorded in `CLAUDE.md` and the farming-area work
avoids each of them by name.

---

## Testing

There is no test framework. `demo()` in `minimap_bot.py` is the test suite —
assert-based, runnable with no game, no gamepad and no board.

```
python minimap_bot.py --demo
python memscan.py --demo
```

Both pass. Note that `--demo` prints one line that looks like a failure:

```
gave up returning to area 'pen' (434 units away) -- confinement OFF
```

That is the wrong-map test proving it works. `demo ok` on the following line is
the result.

## Still open

Target switching is frequent and not yet explained. `--targetlog` now prints
*why* each switch happened (killed, left the list, out of the area,
blacklisted, or something genuinely nearer) — those look identical on screen and
need different fixes, so the reason is logged rather than guessed at.
