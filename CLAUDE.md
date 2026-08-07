# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A screen-reading combat bot for the game **SpiritVale** (Windows). It captures the
game's minimap, finds red monster dots, and drives a gamepad left stick toward the
nearest one while holding the attack button and recasting a d-pad buff on a timer.
No game memory reading, no injection — pure screen capture + synthetic input.

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
```

There is no test framework. `demo()` in `minimap_bot.py` **is** the test suite: an
assert-based self-check on synthetic images plus a mocked serial port, runnable
with no game, no gamepad, no board. Extend it when adding vision or protocol logic;
`python minimap_bot.py --demo` must stay green. Arg parsing is hand-rolled `sys.argv`
scanning at the bottom of the file — no argparse in `minimap_bot.py`.

## Architecture

`minimap_bot.py` is the whole bot, single file, three layers:

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
3. **Pad backends** — `VirtualPad` (vgamepad/ViGEmBus, XInput) and `ArduinoPad`
   (serial to a Leonardo). Duck-typed, same three methods: `stick(sx, sy, attack)`,
   `tap_dpad(name, hold)`, `close()`. Pick a backend by adding a class with those
   three methods; nothing else in the file knows the difference.

Tuning constants all sit in one block at the top of `minimap_bot.py`. Prefer
adjusting them over adding code paths.

## Hard-won constraints — do not "simplify" these away

- **The game only reads XInput, not generic HID.** The Arduino path works over
  serial and the board enumerates fine, but the game ignores it. vgamepad is the
  working path; the Arduino backend is kept as a fallback, not the default.
- **The game sits in keyboard mode until it sees stick motion**, and it *drops the
  first button press* during the mode swap. Every button sequence must be preceded
  by `wake_controller(pad)` (stick nudge + `WAKE_SETTLE_S`). Removing it silently
  eats the leading d-pad press.
- **Red blobs within `CONCEAL_PX` of the box centre are never targets** — that is
  either "arrived" or a fixed red UI element, and chasing it freezes the bot.
- **Do not reintroduce marker detection *for our own character*.** Finding it as
  the nearest white blob failed two ways: the marker turns blue in a party, and in
  a crowd the nearest white blob belongs to another player. The centre is simpler
  and strictly more reliable. `find_white_players()` is the opposite job and is
  fine — it looks for *other* players to pair pets with, and excludes anything
  within `CONCEAL_PX` of the centre precisely so our own marker cannot qualify.
- **The white player dot only renders in about half the frames.** Anything keyed
  on it needs memory across frames, which is why `PetFilter` confirms a pair and
  then tracks the pet by its own red marker. Do not "simplify" it to a plain
  distance test — the pet reappears as a target every other frame if you do.
- **`START_PAUSED` must stay true.** Launching the script has to be safe; the bot
  waits for `Delete` (`TOGGLE_VK`) before it touches the stick.
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

`minimap_navigator.py` is an earlier, superseded navigation experiment (argparse,
pywin32, proportional stick) kept for reference — new work goes in `minimap_bot.py`.

## Style

The codebase follows ponytail conventions: shortest thing that works, stdlib and
already-installed deps before new ones, and `# ponytail:` comments marking
deliberate simplifications and their upgrade path. Comments explain *why* a
non-obvious constant or branch exists (usually a game quirk), not what the code does.
