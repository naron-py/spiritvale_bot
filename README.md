# spiritvale-bot

Screen-reading combat bot for SpiritVale. Watches the minimap, walks the character
to the nearest red monster dot with the left stick, holds the attack button, taps
a spam button on a fast timer, and recasts a d-pad buff sequence on a slow one.
Everything runs while the chase continues — the bot never stands still to cast.

It keeps hold of one target rather than re-picking the nearest every frame, skips
other players' pets, never stands still between kills, and gives up on a monster it
cannot reach instead of walking into a wall forever.

If the server drops you it logs back in by itself: **Ok** on the disconnect modal,
then the **SEA** server, then **Play Character** — with the mouse, since those
screens do not take the gamepad. Set `RECONNECT = False` to turn that off.

All tuning lives in one constant block at the top of `minimap_bot.py`:
`SPAM_BUTTON` / `SPAM_PERIOD_S` for the spammed button (`None` disables it),
`BUFF_SEQUENCE` / `BUFF_PERIOD_S` for the buff, `ATTACK_MASH` to tap the attack
button instead of holding it.

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

**It starts stopped.** **End** toggles running, from any window. Ctrl+C exits and
centres the stick. Launching the script never moves your character on its own.

Watch what it is tracking, live, in a second terminal:

```
python minimap_bot.py --watch
```

Green circles are targetable dots, red ones are ignored under the player marker,
cyan is the chosen target, and the magenta arrow is the stick vector being sent.
It only reads the screen, so it drives nothing and is safe beside a running bot.

## Calibration

The only thing that needs tuning per machine is the minimap box:

```
python minimap_bot.py --snap
```

Writes `minimap_snap.png`. Cyan cross = the box centre, which the bot takes to be
the character; green circles = monsters, red circles = rejected. Nudge
`MINIMAP = dict(cx, cy, r)` — fractions of the client area — until the cross sits
on your character marker. **Get this right:** the centre *is* the origin now, so a
mis-centred box biases every heading the bot takes.

## Diagnostics

| Command | What it checks |
|---|---|
| `--demo` | Offline self-check. No game, no gamepad. |
| `--snap` | What the vision code sees. Minimap alignment. |
| `--watch` | Live tracking view. Read-only, safe to run beside the bot. |
| `--test` | Walks a circle blind. Isolates pad problems from vision problems. |
| `--buff [hold] [gap]` | Fires the buff sequence once, for timing tuning. |
| `--probe` | Presses every X360 button in turn, named, to find a mapping. |
| `--press` | Fires one control at a time on the Arduino, typed in by hand. |
| `--relogin [--dry]` | Handles the login screen showing now. `--dry` looks without clicking. |

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
- **The game binds XInput slot 0 only — unplug every other controller.** A DS4
  plugged in while Steam was presenting it as XInput took slot 0, our virtual pad
  landed in slot 1, and the bot went completely dead: correct XInput output, game
  focused, character motionless. Nothing in the code had changed. To check, read
  the slots back with `XInputGetState`; ours must be slot 0. This looks exactly
  like a broken bot and is not one.
- Stick tilt is direction-only at full magnitude. Scaling tilt by minimap pixel
  distance made every approach a crawl the game's own deadzone swallowed.
- **The server list reorders, so SEA is found by its label, not its position.**
  The table is sorted by ping and the row moves between sessions. `sea_row.png` is
  a crop of the "Southeast Asia (SEA)" text, rescaled to the live client width
  before matching — cut at 1920 wide, it scores 0.98 against a 2560 screen and
  0.34 against a screen without the text. If the label is not found the bot clicks
  nothing and says so: sitting on the server screen beats joining whichever region
  happens to occupy that row today.
- **Connect and Play Character are told apart by width, not position.** They sit
  0.033 of the screen height apart, so a position-only match takes each for the
  other — found on a live disconnect, where the character screen read as the
  server screen and the flow stalled re-clicking Connect. Widths are 0.081 and
  0.147, so size settles it.
- **The login screens are found by button-plus-backdrop, never by one pixel.** The
  sky is the same blue as the buttons, and the skill bar sits where "Play Character"
  does, so a fixed probe point matches during ordinary play — and a false positive
  here means clicking the mouse mid-fight. Each screen therefore needs its button
  *and* something only that screen has behind it: a dark modal body, the white
  server table, or the near-black character backdrop. The dark-modal probes sit
  well clear of the Ok button, which spans x 0.464–0.536; sampling beside its edge
  left 0.004 of margin. Verified against both real screenshots, the server screen
  underneath, and a live gameplay frame that must stay `None`.
- **The bot never waits for a kill to finish — it cannot tell that it needs to.**
  Standing on a monster hides its dot inside `CONCEAL_PX`, which looks exactly like
  the monster dying. There is no signal to separate them, because **your own pet
  follows you and sits inside that radius permanently**, so anything asking "is
  something still under me" answers yes forever. A 0.5s timer was tried first, then
  checking the concealed dots directly — that one parked the bot for a full 6s after
  every kill, the pet being the only thing it ever saw. Attack is held continuously
  regardless, so the bot just walks to the next target: 0.15s, every time.
- **Pets share the monster colour — they are told apart by who they stand next to.**
  Another player's pet is the same red dot as a monster, so it is paired with that
  player's small white dot instead. Two things make this survivable: a pair must
  hold for `PET_CONFIRM_FRAMES` before it counts, so a monster merely walking past
  a player is not written off; and once confirmed the pet is followed by its *own*
  marker for `PET_FORGET_FRAMES`. That memory is not optional — measured live, the
  white player dot only renders in about half the frames, so a pet identified by
  proximity alone would flicker back into the target list every other frame.
- **The character is assumed to be at the box centre; the marker is never detected.**
  It used to be found as the nearest white blob, which broke twice: the marker turns
  **blue in a party**, and in a crowd the nearest white blob is another player's dot,
  so the bot navigated from somebody else's position. The game pins the character to
  the middle of its minimap, so the centre is the answer — and it costs no pixels.
- Red blobs under the player marker are never targeted — that is either "arrived"
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
- `pad_press.py` — press one control on the Leonardo, for Steam's setup wizard.
- `minimap_navigator.py` — earlier navigation experiment.
- `arduino_joystick_leonardo_v1.ino` — HID gamepad sketch for a Leonardo/Pro Micro.
