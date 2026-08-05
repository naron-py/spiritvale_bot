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
| `--test` | Walks a circle blind. Isolates pad problems from vision problems. |
| `--buff [hold] [gap]` | Fires the buff sequence once, for timing tuning. |
| `--probe` | Presses every X360 button in turn, named, to find a mapping. |

## Gotchas found the hard way

- **The game stays in keyboard mode until it sees stick motion**, and it drops the
  first button press while swapping modes. Every button sequence is preceded by a
  stick nudge plus a settle delay (`WAKE_*` constants).
- **The game reads XInput, not generic HID.** The Arduino Leonardo path talks fine
  over serial but the game ignores it. Use vgamepad for movement.
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
