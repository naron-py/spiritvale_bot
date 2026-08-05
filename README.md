# spiritvale-bot

Screen-reading combat bot for SpiritVale. Watches the minimap, walks the character
to the nearest red monster dot with the left stick, holds the attack button, and
recasts a d-pad buff sequence on a timer.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`vgamepad` needs the **ViGEmBus** driver (one-time install, ships with the package).
`pyserial` is only needed for the Arduino backend.

## Run

```
python minimap_bot.py                    # virtual X360 pad (this is the one that works)
python minimap_bot.py --port auto        # Arduino Leonardo over serial instead
```

**End** toggles pause. Ctrl+C stops and centres the stick.

Watch what it is tracking, live, in a second terminal:

```
python minimap_bot.py --watch
```

Green circles are targetable dots, red ones are ignored under the player arrow,
cyan is the chosen target, and the magenta arrow is the stick vector being sent.
It only reads the screen, so it drives nothing and is safe beside a running bot.

## Calibration

The only thing that needs tuning per machine is the minimap box:

```
python minimap_bot.py --snap
```

Writes `minimap_snap.png`. Magenta cross = box centre, cyan cross = the detected
white player arrow, green circles = monsters, red circles = rejected. Nudge
`MINIMAP = dict(cx, cy, r)` — fractions of the client area — until the box is
centred on the arrow. Exact centring is not critical: the arrow is re-detected
every frame and used as the real origin, the box only has to contain it.

## Diagnostics

| Command | What it checks |
|---|---|
| `--demo` | Offline self-check. No game, no gamepad. |
| `--snap` | What the vision code sees. Minimap alignment. |
| `--watch` | Live tracking view. Read-only, safe to run beside the bot. |
| `--test` | Walks a circle blind. Isolates pad problems from vision problems. |
| `--buff [hold] [gap]` | Fires the buff sequence once, for timing tuning. |
| `--probe` | Presses every X360 button in turn, named, to find a mapping. |

## Gotchas found the hard way

- **The game stays in keyboard mode until it sees stick motion**, and it drops the
  first button press while swapping modes. Every button sequence is preceded by a
  stick nudge plus a settle delay (`WAKE_*` constants).
- **The game reads XInput, not generic HID.** Confirmed the long way: flash
  `arduino_joystick_leonardo_v1.ino`, and Windows enumerates a HID-compliant game
  controller (VID 2341 PID 8036 MI_02) whose axes read back correctly through
  `winmm.joyGetPosEx` — full deflection on all four directions. The character
  still does not move. The Leonardo cannot drive this game without XInput
  firmware (a different USB core and bootloader entirely). **Use vgamepad.**
  Note the board ships answering `P` with `PONG` on *either* sketch, so a
  handshake alone does not prove the joystick firmware is flashed. `L`/`V` are
  the commands that tell them apart — the mouse/keyboard sketch rejects both
  with `ERROR:UNKNOWN_CMD`.
- **Steam's generic-gamepad mapping does not rescue the Arduino either.** Steam
  detects the board as "LLC Arduino Leonardo" once Generic Gamepad Configuration
  Support is on, and its setup wizard can be answered with `pad_press.py`. The
  character still does not move. Measured, same focus and session, only the pad
  differing: minimap frame-delta 10.1 for vgamepad, 2.1 for the Arduino against
  a 2.35 idle floor. Enabling Steam Input to force a translation layer is the
  one untried route, and it breaks the vgamepad path it would replace.
- Stick tilt is direction-only at full magnitude. Scaling tilt by minimap pixel
  distance made every approach a crawl the game's own deadzone swallowed.
- Red blobs under the player arrow are never targeted — that is either "arrived"
  or a fixed red UI element, and treating it as a target froze the bot.
- **Monsters are told from red mushroom terrain art by saturation, not by size.**
  Dots render pure red (S 255), mushrooms are desaturated pink (S 94–154), hence
  `RED_S_MIN`. Size cannot work: an occluded cap is a dot-sized sliver, and dots
  that cluster merge into one cap-sized blob. `--snap` prints each blob's width
  as a diagnostic only — do not filter on it or packed monsters vanish.
- Buffs fire one d-pad press per loop pass while the chase continues. Standing
  still for the whole sequence lost several seconds of uptime every minute.

## Files

- `minimap_bot.py` — the bot.
- `minimap_navigator.py` — earlier navigation experiment.
- `arduino_joystick_leonardo_v1.ino` — HID gamepad sketch for a Leonardo/Pro Micro.
