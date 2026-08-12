"""SpiritVale minimap bot: chase nearest red dot with left stick.

deps: pip install mss opencv-python numpy pygetwindow
  virtual pad:  pip install vgamepad     (needs the ViGEmBus driver, one-time)
  real HID pad: pip install pyserial     (Leonardo running arduino_joystick_leonardo_v1.ino)

usage:
  python minimap_bot.py               # vgamepad
  python minimap_bot.py --port COM5   # Arduino Leonardo
  python minimap_bot.py --demo        # offline self-check
"""
import os
import sys
import time

import cv2
import numpy as np

WINDOW_TITLE = "SpiritVale"
# Square capture box centred on the player arrow. cx/cy are fractions of the
# client area, r is a fraction of client width (the chase radius).
# ponytail: this is the calibration knob -- run --snap and nudge until the cyan
# cross lands on the white arrow.
MINIMAP = dict(cx=0.927, cy=0.152, r=0.055)
SPEED = 1.0              # stick magnitude while chasing, 0..1
DEADZONE_PX = 4          # blob this close to center = arrived
# Dots this close to the character are never targeted: a monster we have arrived
# at, or a fixed red UI element at the centre. It was briefly widened to 35 to
# swallow the player's own pet as well, which does not work -- the pet wanders off
# to pick up items, so no radius covers it without blinding the bot to real
# monsters. Excluding the pet needs to know which dot it is, not where it is.
CONCEAL_PX = 20
LOST_HOLD_S = 1.0        # keep last heading this long after a dot vanishes
TARGET_TRACK_PX = 20     # same marker can move this far between 20Hz captures
TARGET_ARRIVE_PX = 28    # near-centre disappearance means arrival; keep > CONCEAL_PX
TARGET_FLICKER_FRAMES = 2  # tolerate this much point-blank occlusion, no more
STUCK_TIMEOUT_S = 3.0    # no meaningful approach this long = inaccessible target
STUCK_PROGRESS_PX = 4    # cumulative distance gain that restarts the timeout
STUCK_MIN_DIST_PX = 35   # never abandon a target already within attack approach range
TARGET_IGNORE_S = 8.0    # retry an inaccessible monster later in case it moved
ATTACK_MASH = False      # False = hold L1 down; True = tap it on the cycle below
ATTACK_PERIOD_S = 0.40   # mash cycle, ignored while ATTACK_MASH is False
ATTACK_HOLD_S = 0.15     # how long L1 stays down each mash cycle
BUFF_PERIOD_S = 60.0     # recast the buff sequence this often
BUFF_SEQUENCE = ("up", "left", "down", "right")
SPAM_BUTTON = "y"       # button tapped on its own timer while running; None = off
SPAM_PERIOD_S = 2
SPAM_HOLD_S = 0.05
WAKE_AMP = 0.5           # stick nudge that flips the game into controller mode
WAKE_STEP_S = 0.15
WAKE_SETTLE_S = 0.5      # grace after the nudge before the first button press
BUFF_HOLD_S = 0.25       # each d-pad press
BUFF_GAP_S = 0.80        # pause between presses -- must outlast the cast animation
MIN_BLOB_AREA = 6        # px, filters compression speckle
# ponytail: the box centre is the player, so MINIMAP cx/cy is now load-bearing --
# it used to only have to contain the marker. Run --snap after any UI change.
# Red mushroom caps painted on the terrain are what the bot kept walking to.
# Size cannot separate them -- an occluded cap is a sliver the size of a dot, and
# merged dots are the size of a cap. Colour can: monster dots are drawn pure red
# (H 0, S 255), every mushroom pixel is desaturated pink (S 94-154). Anything
# under this floor is terrain art. Sample a stray blob's HSV before touching it.
RED_S_MIN = 200
WHITE_S_MAX = 65         # player dots are bright neutral white
WHITE_V_MIN = 190
PLAYER_AREA = (20, 120)  # measured 8-10px dots; rejects large white UI artwork
PLAYER_SIDE = (5, 13)
PET_NEAR_PX = 18         # pet centres measured 11.8px and 14.8px from their player
PET_RELEASE_PX = 24      # hysteresis: do not flicker at the entry threshold
PET_CONFIRM_FRAMES = 3   # a monster crossing a player for one frame stays targetable
PET_TRACK_STEP_PX = 20   # maximum marker movement between 20Hz captures
PET_FORGET_FRAMES = 40   # remember a vanished confirmed pet for about two seconds
# Reconnect after a disconnect: click Ok, pick the server, play the character.
# Every coordinate is a fraction of the client area, measured off screenshots taken
# at two different resolutions (1920x1080 and 2560x1440), so they survive a
# resolution change the same way MINIMAP does. Buttons are all centred on x.
RECONNECT = True          # False disables the whole thing, clicks included
RECONNECT_POLL_S = 2.0    # how often to look for a login screen while running
RECONNECT_SETTLE_S = 1.5  # wait after each click; these screens animate
UI_BLUE = ((95, 90, 150), (112, 255, 255))    # the game's button blue, in HSV
# (x, y, width) fractions. Width matters: Connect and Play sit only 0.033 apart
# vertically, close enough that either matches the other on position alone -- the
# first live test read the character screen as the server screen for exactly that
# reason. Their widths are nearly 2x apart, so size is what separates them.
OK_BTN = (0.500, 0.144, 0.073)       # "Ok" on the disconnected modal
CONNECT_BTN = (0.500, 0.915, 0.081)  # "Connect" under the server table
PLAY_BTN = (0.500, 0.948, 0.147)     # "Play Character" on the character screen
# The server table reorders itself, so SEA has no fixed row -- it is found by its
# own label. The template is cut from a 1920-wide screenshot and rescaled to the
# live client width; matching a simulated 2560 screen scores 0.98, and a screen
# without the text scores 0.34. Recut it if the game restyles the list.
SEA_ICON = "sea_row.png"
SEA_REF_W = 1920
SEA_MATCH_MIN = 0.70
# Dark modal body, sampled either side of the message text. Well clear of the Ok
# button, which spans x 0.464-0.536: probing right beside its edge left 0.004 of
# margin, which is no margin at all.
MODAL_DARK = ((0.44, 0.093), (0.55, 0.093))
PANEL_WHITE = (0.42, 0.60)    # white body of the server table
CHAR_BG = ((0.30, 0.50), (0.50, 0.06), (0.70, 0.85), (0.06, 0.60))  # dark backdrop
# Targeting from the game's own unit list rather than from red pixels. It knows
# what a thing IS -- monster, pet, player -- so the pet, other players' pets,
# mushroom terrain art and the minimap's rotation all stop mattering at once.
# Falls back to the screen path when the offsets go stale, which a patch does.
MEMORY_TARGETING = True
MEM_REFRESH_S = 2.0      # rediscovering units scans GBs; positions are re-read
MEM_RANGE = 70.0         # world units; roughly what the minimap used to cover
MEM_CAL_PUSH_S = 0.7     # per calibration push, two of them
MEM_CAL_MIN = 0.5        # world units a push must move us to count
MEM_CAL_LEGS = 3         # pushes to fit the basis from; more resists shoving
TARGET_SWITCH = 0.7      # only swap targets for one this much nearer
# Standing on the monster, the direction to it flips every frame -- measured at a
# median of 0.4 world units, which is the bot wiggling left and right on the
# spot. Inside this, stop steering and just hit it.
MEM_ARRIVE = 2.5         # world units
# A target that will not die -- already dead and still listed, unreachable, or
# simply not attackable -- otherwise parks the bot on the spot forever, swinging
# at it. Give up on one after this long and leave it alone for a while.
MEM_ENGAGE_MAX_S = 8.0
MEM_IGNORE_S = 20.0
CAL_RETRY_S = 15.0       # wait this long before trying to calibrate again
# A position read can come back empty for a frame without our unit being gone.
# Tearing the calibration down on the first miss cost a whole run: the bot went
# silent and only a double-End brought it back. Insist on a run of misses.
MEM_LOST_FRAMES = 5
# The anchor/leash/patrol feature was cut as buggy: End is a plain toggle again
# and the bot roams wherever the kills lead. It is in git history if the idea is
# revisited -- the minimap scale tracking went with it, since sizing the leash
# circle in world units was the only thing it was ever for.
TOGGLE_VK = 0x23         # End, polled globally through GetAsyncKeyState
START_PAUSED = True      # launching the script must never move the character
LOOP_HZ = 20


def wake_controller(pad):
    """SpiritVale stays in keyboard mode until it sees stick motion, and button
    presses sent before that are dropped. A there-and-back nudge flips it."""
    for sx, sy in ((0.0, WAKE_AMP), (0.0, -WAKE_AMP), (0.0, 0.0)):
        pad.stick(sx, sy, False)
        time.sleep(WAKE_STEP_S)
    # The game eats the first button press while it swaps input modes, which is
    # why the leading d-pad press went missing. Let the swap finish.
    time.sleep(WAKE_SETTLE_S)


def toggle_key_hit(get_state=None):
    """True once per physical End press, even when another window has focus.

    GetAsyncKeyState's low bit means 'pressed since the last call', so polling it
    is already edge-detected -- no key hook, no extra dependency.
    """
    if get_state is None:
        import ctypes
        get_state = ctypes.windll.user32.GetAsyncKeyState
    return bool(get_state(TOGGLE_VK) & 1)


def toggle_running(paused, pad, pet_filter, wake=wake_controller):
    """Toggle run state, always clearing held controls and stale target state."""
    paused = not paused
    pad.stick(0.0, 0.0, False)
    pet_filter.reset()
    if not paused:
        wake(pad)
    return paused


def find_window():
    import pygetwindow as gw
    wins = [w for w in gw.getWindowsWithTitle(WINDOW_TITLE) if w.title.strip() == WINDOW_TITLE]
    if not wins:
        wins = gw.getWindowsWithTitle(WINDOW_TITLE)
    if not wins:
        raise RuntimeError(f"no window titled {WINDOW_TITLE!r}")
    return wins[0]


def minimap_region(win):
    r = int(win.width * MINIMAP["r"])
    x = win.left + int(win.width * MINIMAP["cx"])
    y = win.top + int(win.height * MINIMAP["cy"])
    return dict(left=x - r, top=y - r, width=2 * r, height=2 * r)


def window_region(win):
    return dict(left=win.left, top=win.top, width=win.width, height=win.height)


def find_blue_button(img, btn, tol=0.03, wtol=0.35):
    """Centre (x, y) of the blue UI button matching `btn` = (fx, fy, wfrac), or None.

    Matches on size as well as position. Looking for the button at all, rather
    than sampling a pixel, is what keeps the blue sky and the blue skill icons out
    of it; matching its width is what keeps Connect and Play Character apart.
    """
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, UI_BLUE[0], UI_BLUE[1])
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    wx, wy, want = w * btn[0], h * btn[1], w * btn[2]
    for c in cnts:
        if cv2.contourArea(c) < w * h * 1e-3:  # skip small blue icons and text
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        cx, cy = x + bw / 2, y + bh / 2
        if (abs(cx - wx) <= w * tol and abs(cy - wy) <= h * tol and
                abs(bw - want) <= want * wtol):
            return int(cx), int(cy)
    return None


_sea_icon = False  # False = not looked for yet, None = missing


def sea_icon():
    """The SEA label template, or None if the file is absent."""
    global _sea_icon
    if _sea_icon is False:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SEA_ICON)
        _sea_icon = cv2.imread(path)
        if _sea_icon is None:
            print(f"no {SEA_ICON} -- cannot pick the server row")
    return _sea_icon


def find_sea_row(img, template=None):
    """Centre (x, y) of the "Southeast Asia (SEA)" label, or None.

    The list is sorted by ping and reorders between sessions, so the row cannot be
    addressed by position. Clicking the label selects the row it sits in.
    """
    t = sea_icon() if template is None else template
    if t is None:
        return None
    scale = img.shape[1] / SEA_REF_W
    if scale != 1:
        t = cv2.resize(t, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    if img.shape[0] <= t.shape[0] or img.shape[1] <= t.shape[1]:
        return None
    res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < SEA_MATCH_MIN:
        return None
    return loc[0] + t.shape[1] / 2, loc[1] + t.shape[0] / 2


def _probe(img, frac, dark):
    h, w = img.shape[:2]
    px = img[int(h * frac[1]), int(w * frac[0])]
    return int(px.max()) < 90 if dark else int(px.min()) > 200


def login_screen(img):
    """Which login screen is showing: 'disconnected', 'server', 'character', None.

    Each test pairs a button with something only that screen has behind it, so
    ordinary gameplay -- blue sky above, blue skill icons below -- cannot match.
    The disconnect modal sits on top of the server table, hence the order.
    """
    if (find_blue_button(img, OK_BTN) and
            all(_probe(img, f, dark=True) for f in MODAL_DARK)):
        return "disconnected"
    if find_blue_button(img, CONNECT_BTN) and _probe(img, PANEL_WHITE, dark=False):
        return "server"
    if (find_blue_button(img, PLAY_BTN) and
            all(_probe(img, f, dark=True) for f in CHAR_BG)):
        return "character"
    return None


def click_at(x, y):
    """Left click in screen coordinates. mouse_event is ancient but is 3 lines."""
    import ctypes
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    u.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    u.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def reconnect_step(img, win, click=click_at, settle=RECONNECT_SETTLE_S,
                   sea_template=None):
    """Advance the login flow by one screen. Returns what it did, or None.

    Driven by what is on screen rather than a fixed script, so a slow server or a
    missed click just means the same screen is handled again next poll.
    """
    screen = login_screen(img)
    if screen is None:
        return None
    h, w = img.shape[:2]

    def press(frac):
        click(win.left + w * frac[0], win.top + h * frac[1])
        time.sleep(settle)

    if screen == "disconnected":
        press(OK_BTN)
    elif screen == "server":
        sea = find_sea_row(img, sea_template)
        if sea is None:
            # Better to sit on this screen and say so than to click blind and
            # join whichever region happens to be sitting in that row today.
            return "server: SEA row not found"
        click(win.left + sea[0], win.top + sea[1])
        time.sleep(settle)
        press(CONNECT_BTN)
    else:
        press(PLAY_BTN)
    return screen


def find_red_dots(bgr):
    """Return [(x, y, width)] of monster dots, image coords. width = short side."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # red wraps hue 0, so two bands
    mask = cv2.inRange(hsv, (0, RED_S_MIN, 90), (10, 255, 255)) | \
           cv2.inRange(hsv, (170, RED_S_MIN, 90), (180, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        if cv2.contourArea(c) < MIN_BLOB_AREA:
            continue
        # reported for --snap only: dots merge when they cluster, so width is a
        # diagnostic, not a filter -- rejecting fat blobs loses packed monsters.
        width = min(cv2.minAreaRect(c)[1])
        m = cv2.moments(c)
        if m["m00"] == 0:
            continue
        out.append((m["m10"] / m["m00"], m["m01"] / m["m00"], width))
    return out


def find_white_players(bgr):
    """Return small bright-white player dots as (x, y), rejecting white UI art."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, WHITE_V_MIN), (180, WHITE_S_MAX, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        area = cv2.contourArea(c)
        _, _, w, h = cv2.boundingRect(c)
        if not (PLAYER_AREA[0] <= area <= PLAYER_AREA[1] and
                PLAYER_SIDE[0] <= w <= PLAYER_SIDE[1] and
                PLAYER_SIDE[0] <= h <= PLAYER_SIDE[1]):
            continue
        m = cv2.moments(c)
        if m["m00"]:
            out.append((m["m10"] / m["m00"], m["m01"] / m["m00"]))
    return out


class PetFilter:
    """Confirm red/white pairs over time, then return red indexes that are pets."""

    def __init__(self):
        self.states = []

    def reset(self):
        self.states = []

    def pet_indexes(self, reds, whites):
        used_red = set()
        next_states = []
        pets = set()

        # White proximity establishes identity. Once established, follow the red
        # marker itself: pets often roam well beyond the initial 18px pairing radius.
        for state in (s for s in self.states if s["confirmed"]):
            steps = state["misses"] + 1
            vx, vy = state.get("velocity", (0.0, 0.0))
            predicted = (state["red"][0] + vx * steps,
                         state["red"][1] + vy * steps)
            choices = [(((red[0] - predicted[0]) ** 2 +
                         (red[1] - predicted[1]) ** 2) ** 0.5, i, red)
                       for i, red in enumerate(reds) if i not in used_red]
            best = min(choices, default=None)
            allowance = PET_TRACK_STEP_PX * min(steps, 2)
            if best is not None and best[0] <= allowance:
                _, i, red = best
                used_red.add(i)
                velocity = ((red[0] - state["red"][0]) / steps,
                            (red[1] - state["red"][1]) / steps)
                white = min(whites,
                            key=lambda p: (p[0] - red[0]) ** 2 +
                                          (p[1] - red[1]) ** 2,
                            default=state["white"])
                next_states.append(dict(red=red, white=white,
                                        frames=state["frames"] + 1,
                                        confirmed=True, misses=0,
                                        velocity=velocity))
                pets.add(i)
            elif state["misses"] < PET_FORGET_FRAMES:
                stale = dict(state)
                stale["misses"] += 1
                next_states.append(stale)

        # Unconfirmed dots still need a nearby white player for three frames.
        pairs = []
        for i, red in enumerate(reds):
            if i in used_red:
                continue
            white = min(whites,
                        key=lambda p: (p[0] - red[0]) ** 2 + (p[1] - red[1]) ** 2,
                        default=None)
            if white is None:
                continue
            separation = ((white[0] - red[0]) ** 2 +
                          (white[1] - red[1]) ** 2) ** 0.5
            if separation <= PET_RELEASE_PX:
                pairs.append((i, red, white, separation))

        used = set()
        for i, red, white, separation in sorted(pairs, key=lambda p: p[3]):
            best = None
            for j, state in enumerate(self.states):
                if state["confirmed"] or j in used:
                    continue
                allowance = PET_TRACK_STEP_PX * (state["misses"] + 1)
                red_step = ((red[0] - state["red"][0]) ** 2 +
                            (red[1] - state["red"][1]) ** 2) ** 0.5
                white_step = ((white[0] - state["white"][0]) ** 2 +
                              (white[1] - state["white"][1]) ** 2) ** 0.5
                if red_step <= allowance and white_step <= allowance:
                    score = red_step + white_step
                    if best is None or score < best[0]:
                        best = (score, j, state)

            if best is not None:
                _, j, old = best
                used.add(j)
                frames = old["frames"] + 1
                confirmed = old["confirmed"] or frames >= PET_CONFIRM_FRAMES
                steps = old["misses"] + 1
                velocity = ((red[0] - old["red"][0]) / steps,
                            (red[1] - old["red"][1]) / steps)
            elif separation <= PET_NEAR_PX:
                frames, confirmed = 1, PET_CONFIRM_FRAMES <= 1
                velocity = (0.0, 0.0)
            else:
                continue  # release-band pairs cannot start a new pet track

            next_states.append(dict(red=red, white=white, frames=frames,
                                    confirmed=confirmed, misses=0,
                                    velocity=velocity))
            if confirmed:
                pets.add(i)

        # Survive a brief marker flicker without forgetting a confirmed pair.
        for j, state in enumerate(self.states):
            if not state["confirmed"] and j not in used and state["misses"] < 2:
                stale = dict(state)
                stale["misses"] += 1
                next_states.append(stale)
        self.states = next_states
        return pets


def nearest(dots, cx, cy):
    return min(dots, key=lambda d: (d[0] - cx) ** 2 + (d[1] - cy) ** 2, default=None)


class TargetLock:
    """Keep one red marker until it is genuinely lost instead of switching nearest.

    The bot never stands still waiting for a kill to finish. Standing on a monster
    hides its dot inside CONCEAL_PX, and there is no way to tell that from the
    monster having died: our own pet follows the character and sits in that same
    radius permanently, so any "is something still under me" test says yes forever.
    Waiting on it parked the bot for seconds after every kill. Attack is held
    continuously anyway, so walking straight to the next target keeps hitting.
    """

    def __init__(self):
        self.target_id = 0
        self.reset()

    def reset(self):
        self.current = None
        self.misses = 0
        self.concealed = False

    def pick(self, dots, cx, cy):
        self.concealed = False
        if self.current is None:
            return self._acquire(dots, cx, cy)

        dot = nearest(dots, self.current[0], self.current[1])
        if dot is not None:
            step = ((dot[0] - self.current[0]) ** 2 +
                    (dot[1] - self.current[1]) ** 2) ** 0.5
            if step <= TARGET_TRACK_PX:
                self.current = dot
                self.misses = 0
                return dot

        self.misses += 1
        # Only point-blank occlusion is worth waiting out, and that lasts a frame
        # or two. Anything longer is a pause the bot cannot justify.
        if self.misses <= TARGET_FLICKER_FRAMES:
            self.concealed = ((self.current[0] - cx) ** 2 +
                              (self.current[1] - cy) ** 2) ** 0.5 <= TARGET_ARRIVE_PX
            return None

        return self._acquire(dots, cx, cy)

    def _acquire(self, dots, cx, cy):
        self.current = nearest(dots, cx, cy)
        self.misses = 0
        self.concealed = False
        if self.current is not None:
            self.target_id += 1
        return self.current


class StuckWatchdog:
    """Report a locked target that has not become meaningfully closer in time."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.target_id = None
        self.best_distance = None
        self.last_progress = None

    def update(self, target_id, dot, cx, cy, now=None):
        now = time.time() if now is None else now
        distance = ((dot[0] - cx) ** 2 + (dot[1] - cy) ** 2) ** 0.5
        if target_id != self.target_id:
            self.target_id = target_id
            self.best_distance = distance
            self.last_progress = now
            return False
        if distance <= STUCK_MIN_DIST_PX:
            self.best_distance = min(self.best_distance, distance)
            self.last_progress = now
            return False
        if distance <= self.best_distance - STUCK_PROGRESS_PX:
            self.best_distance = distance
            self.last_progress = now
        return now - self.last_progress >= STUCK_TIMEOUT_S


class TargetBlacklist:
    """Temporarily follow and exclude red markers found to be inaccessible."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.tracks = []

    def block(self, dot, now=None):
        now = time.time() if now is None else now
        self.tracks.append(dict(dot=dot, until=now + TARGET_IGNORE_S))

    def filter(self, dots, now=None):
        now = time.time() if now is None else now
        tracks = [track for track in self.tracks if now < track["until"]]
        blocked = set()
        for track in tracks:
            choices = [((((dot[0] - track["dot"][0]) ** 2 +
                           (dot[1] - track["dot"][1]) ** 2) ** 0.5), i, dot)
                       for i, dot in enumerate(dots) if i not in blocked]
            best = min(choices, default=None)
            if best is not None and best[0] <= TARGET_TRACK_PX:
                _, i, dot = best
                track["dot"] = dot
                blocked.add(i)
        self.tracks = tracks
        return [dot for i, dot in enumerate(dots) if i not in blocked]


def pick_target(img, pet_filter=None, target_lock=None,
                target_blacklist=None, now=None):
    """(origin, targetable dots, chosen dot) -- the whole targeting rule, once.

    main() and --watch both call this, so the live view cannot drift out of step
    with what the bot is actually chasing.
    """
    h, w = img.shape[:2]
    # The game keeps the character pinned to the middle of its minimap, so the box
    # centre IS the player. Detecting the marker instead was fragile: it turns blue
    # in a party, and in a crowd the nearest white blob is somebody else's dot.
    cx, cy = w / 2, h / 2
    # Blobs under the player marker are never a target: we have arrived, or it is
    # our own pet, or a fixed red UI element at the centre. Keeping them to detect
    # "still fighting" was tried and reverted -- our pet never leaves that radius,
    # so it read as an endless fight and stalled the bot after every kill.
    red = find_red_dots(img)
    if pet_filter is not None:
        # Our own marker may also be white when not in a party. It is fixed at the
        # box centre and must not make a nearby real monster look like somebody's pet.
        players = [p for p in find_white_players(img)
                   if (p[0] - cx) ** 2 + (p[1] - cy) ** 2 > CONCEAL_PX ** 2]
        # One call per frame: pet_indexes advances a tracker, so calling it twice
        # would double-step every confirmation counter.
        pets = pet_filter.pet_indexes(red, players)
        red = [d for i, d in enumerate(red) if i not in pets]
    dots = [d for d in red
            if (d[0] - cx) ** 2 + (d[1] - cy) ** 2 > CONCEAL_PX ** 2]
    if target_blacklist is not None:
        dots = target_blacklist.filter(dots, now)
    chosen = target_lock.pick(dots, cx, cy) if target_lock else nearest(dots, cx, cy)
    return (cx, cy), dots, chosen


def stick_for(basis, dx, dz):
    """Stick (sx, sy) that walks toward a world offset, or None if unsolvable.

    `basis` maps a stick push to the world travel it produces, measured live:
    [[wx_for_sx, wx_for_sy], [wz_for_sx, wz_for_sy]]. Inverting it answers the
    question the bot actually has -- which way to push to go there. Doing it this
    way means the world's axes and the camera's never have to be known.
    """
    (a, b), (c, d) = basis
    det = a * d - b * c
    if abs(det) < 1e-9:
        return None                    # the two pushes were parallel: no basis
    sx = (d * dx - b * dz) / det
    sy = (-c * dx + a * dz) / det
    n = (sx * sx + sy * sy) ** 0.5
    if n < 1e-9:
        return None
    return sx / n * SPEED, sy / n * SPEED


def stale_target(now, engaged_since, limit=MEM_ENGAGE_MAX_S):
    """True once we have been on one target longer than it should take to die.

    Something that will not die -- already dead and still listed, unreachable,
    not attackable -- parks the bot on the spot swinging at it, which is the one
    failure mode that looks exactly like a hung bot from the outside.
    """
    return engaged_since is not None and now - engaged_since > limit


def nearest_monster(units, px, pz, reach=MEM_RANGE):
    """Closest unit that is actually a monster. Pets are simply not monsters."""
    best, best_d = None, reach
    for kind, addr, x, y, z in units:
        if kind != "monster":
            continue
        d = ((x - px) ** 2 + (z - pz) ** 2) ** 0.5
        if d < best_d:
            best, best_d = (addr, x, y, z), d
    return (best, best_d) if best else (None, None)


def pick_me(legs, floor=MEM_CAL_MIN, need=MEM_CAL_LEGS):
    """Which unit is the one answering the stick?

    `legs` is [((sx, sy), {addr: (dx, dz)})] from the calibration pushes. The obvious test -- whoever moved furthest -- is wrong on a busy
    map, where another player simply walks faster than our pushes and steals
    the identification. What is true of us and of nobody else is that our
    travel is a *linear function of the stick*: push east twice as hard, go
    twice as far; push the other way, come back. A player going about their
    own business fits that badly no matter how fast they move, so score every
    unit by how well a single 2x2 basis explains all of its legs at once and
    take the best fit.
    """
    per = {}
    for stick, moved in legs:
        for addr, d in moved.items():
            per.setdefault(addr, []).append((stick, d))
    best, best_err = None, None
    for addr, obs in per.items():
        live = [(s, d) for s, d in obs if (d[0] ** 2 + d[1] ** 2) ** 0.5 >= floor]
        if len(live) < need:
            continue                    # too little to tell motion from noise
        S = np.array([s for s, _ in live], dtype=float)
        W = np.array([d for _, d in live], dtype=float)
        if np.linalg.matrix_rank(S) < 2:
            continue
        fit, *_ = np.linalg.lstsq(S, W, rcond=None)
        # relative residual, so a unit is not rewarded for barely moving
        err = float(np.linalg.norm(S @ fit - W) / max(np.linalg.norm(W), 1e-6))
        if best_err is None or err < best_err:
            best, best_err = addr, err
    return best


def stick_vector(dx, dy, radius):
    """Screen delta -> left-stick (x, y), y up positive.

    Direction only, magnitude SPEED. A minimap pixel is many world metres, so
    scaling tilt by pixel distance made every move a crawl the game's own
    deadzone swallowed. ponytail: drop SPEED below 1.0 only if you overshoot.
    """
    n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    scale = SPEED / n
    return float(np.clip(dx * scale, -1, 1)), float(np.clip(-dy * scale, -1, 1))


class MemoryEyes:
    """Targets read from the game's unit list instead of inferred from pixels.

    Discovery is slow -- finding every instance of a class means scanning GBs --
    so it happens on a timer and each frame only re-reads the positions of units
    already found. Spawns show up a refresh late, which at this range is nothing.
    """

    def __init__(self):
        import memscan
        self.ms = memscan
        self.mem = memscan.Mem()
        self.classes = memscan.type_classes(self.mem)
        self.me = None            # our own BaseUnitController
        self.basis = None         # stick push -> world travel
        self.units = []           # cached (kind, addr, x, y, z)
        self.chasing = None       # unit held between frames, so it does not flap
        self.engaged_since = None # when we started on the current target
        self.ignored = {}         # unit -> time it becomes fair game again
        self.mode = "no unit"
        self.misses = 0           # consecutive frames our position did not read
        self.hot = None           # regions worth sweeping
        self.scanner = None
        self.stop = None
        import threading
        self.lock = threading.Lock()

    def available(self):
        return bool(self.classes.get("monster"))

    def close(self):
        if self.stop is not None:
            self.stop.set()
        self.mem.close()

    def _positions(self, addrs):
        out = {}
        for a in addrs:
            p = self.ms.read_vec3(self.mem, a + self.ms.UNIT_POSITION)
            if p and self.ms.looks_like_place(p):
                out[a] = p
        return out

    def calibrate(self, pad):
        """Find which unit is us, and how a stick push maps to world travel.

        Our unit is the one that moves when we push, which is a fact we can
        create on demand rather than infer. It
        works mid-combat, unlike anything built on walking a clean line.
        """
        players = self.known_players()   # from the background sweep, never blocks
        if not players:
            return False

        def push(sx, sy):
            """World delta per unit over one push."""
            before = self._positions(players)
            t0 = time.time()
            while time.time() - t0 < MEM_CAL_PUSH_S:
                pad.stick(sx, sy, False)
                time.sleep(0.05)
            pad.stick(0.0, 0.0, False)
            time.sleep(0.2)
            after = self._positions(players)
            return {a: (after[a][0] - before[a][0], after[a][2] - before[a][2])
                    for a in before if a in after}

        # Push every direction first, then work out which unit was answering.
        # Picking the biggest mover in a leg does not do it: on a busy map
        # another player out-walks us, calibration locks onto them, and every
        # later leg is thrown away as "not us" -- the whole thing then fails
        # with a healthy character standing right there.
        legs = []
        for sx, sy in ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
                       (0.7, 0.7), (-0.7, 0.7)):
            moved = push(sx, sy)
            if moved:
                legs.append(((sx, sy), moved))
        me = pick_me(legs)
        if me is None:
            return False

        samples = []
        for (sx, sy), moved in legs:
            if me not in moved:
                continue
            dx, dz = moved[me]
            dist = (dx * dx + dz * dz) ** 0.5
            if dist < MEM_CAL_MIN:
                continue                    # blocked that way; the wall is fine
            samples.append(((sx, sy), (dx, dz)))
            if len(samples) >= MEM_CAL_LEGS:
                # Least squares over every leg that moved, not just two of them.
                # In a fight the character gets shoved and stunned, so a single
                # leg can be well off -- one bad push used to become the whole
                # basis, and the scale with it.
                S = np.array([s for s, _ in samples], dtype=float)
                W = np.array([w for _, w in samples], dtype=float)
                if np.linalg.matrix_rank(S) < 2:
                    continue                # all pushes along one line so far
                fit, *_ = np.linalg.lstsq(S, W, rcond=None)
                basis = ((float(fit[0][0]), float(fit[1][0])),
                         (float(fit[0][1]), float(fit[1][1])))
                if stick_for(basis, 1.0, 0.0) is None:
                    continue
                self.me, self.basis = me, basis
                return True
        return False

    def start_scanning(self):
        """Rediscover units in the background, forever. Never blocks the bot.

        The first sweep has to read ~8 GB and takes about 14 seconds; narrowed
        afterwards to the regions that actually held units, it drops to about
        one. Both are far too slow for a 20 Hz loop, so this runs on its own
        thread with its own handle and the bot works from pixels until the first
        list lands. Nothing waits.
        """
        import threading
        self.stop = threading.Event()

        def loop():
            mem = self.ms.Mem(self.mem.pid)
            try:
                while not self.stop.is_set():
                    found = self.ms.world_units(mem, regions=self.hot)
                    with self.lock:
                        self.units = found
                    if self.hot is None and found:
                        # Narrow the next sweep to where the units turned out to
                        # be, rather than paying for the whole heap every time.
                        spans = mem.regions()
                        live = {u for _, u, *_ in found}
                        self.hot = [(b, s) for b, s in spans
                                    if any(b <= u < b + s for u in live)]
                    self.stop.wait(MEM_REFRESH_S)
            finally:
                mem.close()

        self.scanner = threading.Thread(target=loop, daemon=True)
        self.scanner.start()

    def known_players(self):
        with self.lock:
            return [u for k, u, *_ in self.units if k == "player"]

    def target(self, now):
        """(sx, sy, distance) toward the nearest monster, or (None, None, None).

        Positions come fresh every call; only the membership list is cached, so a
        monster that walks is chased where it is now, not where it was.
        """
        if not (self.me and self.basis):
            # Say so. Leaving the old mode standing made a bot with no unit at
            # all report whatever it was doing when it still had one, which read
            # as "chasing, but motionless" and sent the hunt to the wrong place.
            self.mode = "no unit"
            return None, None, None
        with self.lock:
            cached = list(self.units)
        live = self._positions([u for _, u, *_ in cached] + [self.me])
        here = live.get(self.me)
        if not here and self.misses < MEM_LOST_FRAMES:
            # One empty read is not a death. Coast on the last known state.
            self.misses += 1
            self.mode = "lost"
            return None, None, None
        if not here:
            # Our unit was rebuilt -- map change, death or relog. Everything
            # derived from it is now meaningless: the basis was measured for a
            # unit that no longer exists. Dropping `hot` forces the next sweep
            # to search
            # the whole heap, because the new objects need not be where the old
            # ones were.
            self.me = self.basis = self.hot = None
            self.chasing = self.engaged_since = None
            self.ignored = {}
            self.mode = "no unit"
            with self.lock:
                self.units = []
            return None, None, None
        self.misses = 0
        px, _, pz = here
        self.mode = "chasing"
        fresh = [(k, u, *live[u]) for k, u, *_ in cached if u in live]
        allowed = [e for e in fresh if self.ignored.get(e[1], 0.0) < now]

        # Hold the current target rather than re-picking the nearest every
        # frame. Two monsters a similar distance away swap which is closer
        # constantly, and the bot answers by walking left, right, left, right
        # instead of going to either. It only switches for something clearly
        # nearer, or when this one is gone.
        held = next(((k, u, x, y, z) for k, u, x, y, z in allowed
                     if u == self.chasing), None)
        hit, dist = nearest_monster(allowed, px, pz)
        if held:
            hd = ((held[2] - px) ** 2 + (held[4] - pz) ** 2) ** 0.5
            if hd <= MEM_RANGE and (not hit or dist > hd * TARGET_SWITCH):
                hit, dist = (held[1], held[2], held[3], held[4]), hd
        if not hit:
            self.chasing = self.engaged_since = None
            self.mode = "no monster"
            return None, None, None
        if hit[0] != self.chasing:
            self.chasing, self.engaged_since = hit[0], now
        elif stale_target(now, self.engaged_since):
            # Long enough on one target that it is not going to die: already
            # dead and still listed, unreachable, or not attackable. Parking on
            # it forever is the one failure that looks exactly like a hung bot.
            self.ignored[hit[0]] = now + MEM_IGNORE_S
            self.chasing = self.engaged_since = None
            self.mode = "gave up"
            return None, None, None
        if dist <= MEM_ARRIVE:
            self.mode = "on it"
            return 0.0, 0.0, dist        # arrived: hold still and swing
        s = stick_for(self.basis, hit[1] - px, hit[3] - pz)
        return (s[0], s[1], dist) if s else (None, None, None)


class VirtualPad:
    """vgamepad backend."""

    def __init__(self):
        import vgamepad as vg
        self.pad = vg.VX360Gamepad()
        self.attack_btn = vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER  # L1 / LB
        self.dpad = {"up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
                     "down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
                     "left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
                     "right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT}
        self.face = {"a": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
                     "b": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                     "x": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
                     "y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y}

    def _tap(self, btn, hold):
        # The stick keeps its last value across this: left_joystick_float persists
        # between updates, so a tap never interrupts the chase.
        self.pad.press_button(btn)
        self.pad.update()
        time.sleep(hold)
        self.pad.release_button(btn)
        self.pad.update()

    def tap_dpad(self, name, hold):
        self._tap(self.dpad[name], hold)

    def tap_button(self, name, hold=SPAM_HOLD_S):
        self._tap(self.face[name], hold)

    def stick(self, sx, sy, attack=False):
        self.pad.left_joystick_float(sx, sy)
        if attack:
            self.pad.press_button(self.attack_btn)
        else:
            self.pad.release_button(self.attack_btn)
        self.pad.update()  # one report per frame

    def close(self):
        self.stick(0.0, 0.0, False)


class ArduinoPad:
    """arduino_joystick_leonardo_v1.ino over serial. 'L<x>,<y>' -> 'OK'."""

    # ATmega32U4 boards: Arduino LLC, Arduino SA, SparkFun Pro Micro.
    VIDS = (0x2341, 0x2A03, 0x1B4F)

    @staticmethod
    def autodetect():
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        for p in ports:
            if p.vid in ArduinoPad.VIDS:
                return p.device
        listing = "\n  ".join(f"{p.device}  {p.description}" for p in ports) or "(none)"
        raise RuntimeError(f"no Arduino found. ports present:\n  {listing}")

    def __init__(self, port=None, baud=115200):
        import serial
        if port in (None, "auto"):
            port = self.autodetect()
            print(f"auto-detected {port}")
        try:
            self.ser = serial.Serial(port, baud, timeout=0.5)
        except serial.SerialException as e:
            from serial.tools import list_ports
            have = ", ".join(p.device for p in list_ports.comports()) or "(none)"
            raise RuntimeError(f"{port} not usable ({e}). ports present: {have}") from None
        # Opening at 115200 does not reset a Leonardo, so waiting for its READY
        # banner just burns the whole timeout on an already-booted board. Ping
        # instead, and keep pinging: a board that IS mid-reset drops the first one.
        deadline = time.time() + 6
        while time.time() < deadline:
            self.ser.write(b"P\n")
            if self.ser.readline().strip() in (b"PONG", b"READY"):
                break
        else:
            raise RuntimeError(f"{port} opened but no PONG -- "
                               f"wrong port, or sketch not flashed")
        self.ser.reset_input_buffer()  # drop READY/PONG backlog before commands
        self.ser.timeout = 2
        self.last = None

    ATTACK_BTN = 4  # LB in the usual XInput button order
    HAT = {"up": 0, "right": 2, "down": 4, "left": 6}  # sketch: 0..7 clockwise from N
    FACE = {"a": 0, "b": 1, "x": 2, "y": 3}  # usual XInput button order

    def tap_dpad(self, name, hold):
        self._cmd(f"V{self.HAT[name]}")
        time.sleep(hold)
        self._cmd("V-1")  # -1 centres the hat

    def tap_button(self, n, hold=None):
        # hold is ignored: the sketch's own B command is press, 50ms, release.
        self._cmd(f"B{self.FACE.get(n, n)}")

    def _cmd(self, line):
        self.ser.write(f"{line}\n".encode())
        reply = self.ser.readline().strip()
        if reply != b"OK":
            print(f"\nboard replied {reply!r} to {line}")

    def stick(self, sx, sy, attack=False):
        # HID Y axis is down-positive, our sy is up-positive.
        x, y = int(sx * 32767), int(-sy * 32767)
        if (x, y, attack) == self.last:
            return  # ponytail: sketch is synchronous, skip no-op round trips
        if self.last is None or (x, y) != self.last[:2]:
            self._cmd(f"L{x},{y}")
        if self.last is None or attack != self.last[2]:
            self._cmd(f"{'D' if attack else 'U'}{self.ATTACK_BTN}")
        self.last = (x, y, attack)

    def close(self):
        self.ser.write(b"Z\n")
        self.ser.readline()
        self.ser.close()


def main(port=None):
    import mss

    win = find_window()
    pad = ArduinoPad(port) if port else VirtualPad()
    print(f"window {win.width}x{win.height} @ ({win.left},{win.top})"
          f" via {type(pad).__name__} -- End to start/stop, ctrl+c to exit")

    last = None  # (t, dist, sx, sy) of last seen dot
    had_unit = False   # so the 'unit rebuilt' notice prints once
    next_cal = 0.0     # earliest retry after a failed calibration
    eyes = None
    if MEMORY_TARGETING:
        try:
            eyes = MemoryEyes()
            if not eyes.available():
                print("MEMORY TARGETING OFF: the class pointers did not resolve."
                      "\n  Almost always a game update -- a patch moves every"
                      " TYPE_RVA in memscan.py (the field offsets usually"
                      " survive).\n  Re-run Il2CppDumper on GameAssembly.dll +"
                      " global-metadata.dat and update those three lines."
                      "\n  Until then: pixels, which means it will chase pets.")
                eyes.close()
                eyes = None
        except Exception as e:                  # game closed, no rights, no dump
            print(f"memory targeting unavailable ({e}); reading pixels instead")
            eyes = None
    pet_filter = PetFilter()
    target_lock = TargetLock()
    target_blacklist = TargetBlacklist()
    stuck_watchdog = StuckWatchdog()
    paused = START_PAUSED
    next_buff = 0.0   # 0 = cast once at startup, then every BUFF_PERIOD_S
    buff_queue = []   # d-pad presses left in the current cast
    next_press = 0.0  # earliest time for the next one
    next_spam = 0.0   # SPAM_BUTTON goes out on its own timer
    next_login_check = 0.0  # a whole-window grab, so kept to RECONNECT_POLL_S

    pad.stick(0.0, 0.0, False)
    print("STOPPED -- press End to start")

    with mss.mss() as sct:
        try:
            while True:
                if toggle_key_hit():
                    paused = toggle_running(paused, pad, pet_filter)
                    target_lock.reset()
                    target_blacklist.reset()
                    stuck_watchdog.reset()
                    last = None
                    buff_queue = []
                    next_buff = next_press = next_spam = 0.0
                    print(f"\n{'STOPPED' if paused else 'STARTED'} (End)")
                    if not paused and eyes is not None and eyes.scanner is None:
                        # Returns at once. The first sweep is slow, so the bot
                        # runs on pixels meanwhile and upgrades itself when the
                        # unit list arrives -- nothing waits on it.
                        eyes.start_scanning()
                        print("scanning for units in the background; "
                              "reading pixels until it lands")
                if paused:
                    time.sleep(0.05)
                    continue

                if RECONNECT and time.time() >= next_login_check:
                    next_login_check = time.time() + RECONNECT_POLL_S
                    full = np.array(sct.grab(window_region(win)))[:, :, :3]
                    if login_screen(full):
                        # Drop the stick and attack before touching the mouse: the
                        # character is gone, and a held button carries into the
                        # next session.
                        pad.stick(0.0, 0.0, False)
                        did = reconnect_step(full, win)
                        print(f"\nreconnect: handled the {did} screen")
                        target_lock.reset()
                        target_blacklist.reset()
                        stuck_watchdog.reset()
                        pet_filter.reset()
                        last = None
                        buff_queue = []
                        next_buff = next_press = next_spam = 0.0
                        continue

                if not buff_queue and time.time() >= next_buff:
                    buff_queue = list(BUFF_SEQUENCE)
                    next_buff = time.time() + BUFF_PERIOD_S
                    print(f"\nbuffing: {' '.join(BUFF_SEQUENCE)}")

                reg = minimap_region(win)
                img = np.array(sct.grab(reg))[:, :, :3]
                h, w = img.shape[:2]
                now = time.time()
                sx = sy = None
                if (eyes is not None and eyes.me is None
                        and now >= next_cal and eyes.known_players()):
                    # The sweep has landed, so the two calibration pushes can
                    # happen now. Two seconds, once, and only after the bot has
                    # already been fighting on pixels rather than before it.
                    print("\nunit list ready -- calibrating (2s)")

                    if eyes.calibrate(pad):
                        had_unit = True
                        print(f"  locked on 0x{eyes.me:012X}; targeting monsters "
                              f"by what they are, not how they look")
                    else:
                        # Usually the character was blocked or stunned and the
                        # pushes moved nothing. Retry later rather than giving up
                        # on memory targeting for the rest of the session.
                        next_cal = now + CAL_RETRY_S
                        print(f"  no unit moved when pushed -- retrying in "
                              f"{CAL_RETRY_S:g}s, pixels until then")

                if eyes is not None:
                    # The unit list knows what each thing IS, so none of the
                    # pixel machinery is needed here: no pet filter, no
                    # saturation threshold for mushrooms, no minimap rotation.
                    msx, msy, mdist = eyes.target(now)
                    if eyes.me is None:
                        # Do not shut memory targeting down for this: it comes
                        # back on its own once the sweep finds the new unit, and
                        # closing it meant one map change dropped the bot onto
                        # pixels for the rest of the session.
                        if had_unit:
                            print("\nour unit was rebuilt (map change, death or "
                                  "relog) -- rediscovering (~15s), pixels "
                                  "until then")
                            had_unit = False
                    elif msx is None:
                        sx = sy = 0.0
                        # Name which kind of nothing this is: "no monster" was
                        # printed for a lost unit too, hiding a dead bot behind
                        # a message that reads like a quiet patch of map.
                        state = {"no unit": "no unit  ",
                                 "lost": "lost     ",
                                 "gave up": "gave up  "}.get(eyes.mode,
                                                             "no monster")
                    else:
                        sx, sy = msx, msy
                        state = ("on it  " if eyes.mode == "on it"
                                 else "dist  ") + f"{mdist:6.1f}"

                # Everything below is the pixel path, used when memory targeting
                # is off or has gone stale. It is left exactly as it was.
                if sx is None:
                    (cx, cy), dots, dot = pick_target(
                        img, pet_filter, target_lock, target_blacklist, now)
                    stuck = (dot is not None and stuck_watchdog.update(
                        target_lock.target_id, dot, cx, cy, now))
                else:
                    dot = stuck = None

                if sx is not None:
                    pass                    # the unit list already decided
                elif stuck:
                    target_blacklist.block(dot, now)
                    target_lock.reset()
                    stuck_watchdog.reset()
                    sx = sy = 0.0
                    last = None
                    state = "stuck skip"
                    print(f"\nstuck target: ignoring for {TARGET_IGNORE_S:g}s")
                elif dot is not None:
                    dx, dy = dot[0] - cx, dot[1] - cy
                    dist = (dx * dx + dy * dy) ** 0.5
                    sx, sy = stick_vector(dx, dy, min(w, h) / 2)
                    last = (now, dist, sx, sy)
                    if dist < DEADZONE_PX:
                        sx = sy = 0.0
                        state = "centered"
                    else:
                        state = f"dist {dist:6.1f}"
                elif target_lock.concealed:
                    # The locked dot vanished near centre: stay put and attack it.
                    sx = sy = 0.0
                    state = "concealed"
                elif last and now - last[0] < LOST_HOLD_S:
                    # brief flicker/occlusion mid-chase: coast on last heading
                    _, _, sx, sy = last
                    state = "coasting"
                else:
                    sx = sy = 0.0
                    last = None
                    state = "no monster"

                # L1 held down continuously. Set ATTACK_MASH to mash it instead.
                atk = (now % ATTACK_PERIOD_S) < ATTACK_HOLD_S if ATTACK_MASH else True
                pad.stick(sx, sy, atk)

                # One d-pad press per pass, spaced by BUFF_GAP_S. The stick and L1
                # keep their last value across a tap, so the buff casts mid-chase
                # instead of parking the bot for a whole sequence.
                key = ""
                if buff_queue and now >= next_press:
                    key = buff_queue.pop(0)
                    pad.tap_dpad(key, BUFF_HOLD_S)
                    next_press = now + BUFF_GAP_S
                elif SPAM_BUTTON and now >= next_spam:
                    # Never in the same pass as a buff press: two taps back to back
                    # land inside one another's animation and the game drops one.
                    pad.tap_button(SPAM_BUTTON, SPAM_HOLD_S)
                    key = SPAM_BUTTON
                    next_spam = now + SPAM_PERIOD_S

                how = "  memory" if (eyes and eyes.me) else "  pixels"
                print(f"{state:12} stick {sx:+.2f},{sy:+.2f} "
                      f"atk {'#' if atk else '.'} {key:5}{how}   ",
                      end="\r")
                time.sleep(1 / LOOP_HZ)
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            pad.close()


def demo():
    """Self-check: synthetic minimap, no game or gamepad needed."""
    assert SPAM_BUTTON is None or SPAM_BUTTON in ArduinoPad.FACE, SPAM_BUTTON

    # Nearest-from-scratch oscillates when two monsters exchange which is a pixel
    # closer. A lock must keep the original marker, then stop rather than coast
    # through it when that marker disappears beneath the player arrow.
    lock = TargetLock()
    left, right = (75.0, 100.0, 8.0), (127.0, 100.0, 8.0)
    assert lock.pick([left, right], 100.0, 100.0) == left
    left, right = (74.0, 100.0, 8.0), (125.0, 100.0, 8.0)
    assert nearest([left, right], 100.0, 100.0) == right
    assert lock.pick([left, right], 100.0, 100.0) == left

    # The target vanishes -- killed, or hidden under the character. Either way the
    # next one is due at once. Waiting to see whether it was really dead cannot
    # work: our own pet sits inside CONCEAL_PX forever, so every test for "is
    # something still under me" answers yes and the bot stands there.
    lock.reset()
    close, competitor = (122.0, 100.0, 8.0), (150.0, 100.0, 8.0)
    assert lock.pick([close, competitor], 100.0, 100.0) == close
    after = [lock.pick([competitor], 100.0, 100.0)
             for _ in range(TARGET_FLICKER_FRAMES + 1)]
    assert after[:-1] == [None] * TARGET_FLICKER_FRAMES, after
    assert after[-1] == competitor and not lock.concealed
    stall = len(after) / LOOP_HZ
    assert stall <= 0.2, f"post-kill stall must stay under 0.2s, got {stall}"

    # A target lost well away from the character is not an arrival: it flickered
    # or died out there, so the bot coasts rather than treating it as reached.
    lock.reset()
    far = (100.0 - TARGET_ARRIVE_PX - 15, 100.0, 8.0)
    assert lock.pick([far], 100.0, 100.0)
    assert lock.pick([], 100.0, 100.0) is None and not lock.concealed

    watchdog = StuckWatchdog()
    blocked = (60.0, 100.0, 8.0)  # 40px away and making no progress
    assert not watchdog.update(1, blocked, 100.0, 100.0, now=0.0)
    assert not watchdog.update(1, blocked, 100.0, 100.0,
                               now=STUCK_TIMEOUT_S - 0.01)
    assert watchdog.update(1, blocked, 100.0, 100.0, now=STUCK_TIMEOUT_S)

    # Four pixels of cumulative progress restarts the timeout.
    watchdog.reset()
    assert not watchdog.update(2, (50.0, 100.0, 8.0), 100.0, 100.0, now=0.0)
    assert not watchdog.update(2, (55.0, 100.0, 8.0), 100.0, 100.0, now=2.9)
    assert not watchdog.update(2, (55.0, 100.0, 8.0), 100.0, 100.0, now=5.8)
    assert watchdog.update(2, (55.0, 100.0, 8.0), 100.0, 100.0, now=5.9)
    watchdog.reset()
    assert not watchdog.update(3, (130.0, 100.0, 8.0),
                               100.0, 100.0, now=99.0)  # near targets never time out

    blacklist = TargetBlacklist()
    other = (150.0, 100.0, 8.0)
    blacklist.block(blocked, now=0.0)
    assert blacklist.filter([blocked, other], now=0.0) == [other]
    moved_blocked = (65.0, 100.0, 8.0)
    assert blacklist.filter([moved_blocked, other], now=1.0) == [other]
    assert blacklist.filter([moved_blocked, other],
                            now=TARGET_IGNORE_S + 0.01) == [moved_blocked, other]

    assert TOGGLE_VK == 0x23 and START_PAUSED

    # Memory targeting. stick_for inverts a measured basis, so the world's axes
    # and the camera's orientation never have to be known -- which is the point:
    # a rotated minimap cannot mislead a heading computed this way.
    ident = ((1.0, 0.0), (0.0, 1.0))          # stick east goes world +x
    sx, sy = stick_for(ident, 3.0, 0.0)
    assert sx > 0.99 and abs(sy) < 0.01, (sx, sy)
    turned = ((0.0, -1.0), (1.0, 0.0))        # world rotated 90 degrees
    sx, sy = stick_for(turned, 0.0, 5.0)      # want +z, must push east
    assert sx > 0.99 and abs(sy) < 0.01, (sx, sy)
    assert stick_for(((1.0, 2.0), (2.0, 4.0)), 1.0, 1.0) is None  # parallel pushes
    assert abs((lambda s: (s[0] ** 2 + s[1] ** 2) ** 0.5)(
        stick_for(ident, 900.0, 900.0)) - SPEED) < 1e-6   # always full tilt

    # a pet is never a target, however close it sits
    around = [("pet", 0xA, 1.0, 0.0, 0.0), ("monster", 0xB, 9.0, 0.0, 0.0),
              ("player", 0xC, 2.0, 0.0, 0.0), ("monster", 0xD, 40.0, 0.0, 0.0)]
    hit, dist = nearest_monster(around, 0.0, 0.0)
    assert hit[0] == 0xB and abs(dist - 9.0) < 1e-6, (hit, dist)
    assert nearest_monster(around, 0.0, 0.0, reach=5.0) == (None, None)
    assert nearest_monster([("pet", 0xA, 1.0, 0.0, 0.0)], 0.0, 0.0) == (None, None)

    # Identifying our own unit among a crowd. The decoy walks further on every
    # single leg -- which is exactly how a busy map broke calibration -- but its
    # travel has nothing to do with the stick, so it must never be chosen.
    _me, _decoy, _idle = 0xAA, 0xBB, 0xCC
    _basis = ((6.0, 1.0), (-1.0, 5.0))
    _legs = []
    for _i, (_sx, _sy) in enumerate(((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0),
                                     (0.0, -1.0))):
        _mine = (_sx * _basis[0][0] + _sy * _basis[1][0],
                 _sx * _basis[0][1] + _sy * _basis[1][1])
        _wander = (14.0, -9.0) if _i % 2 else (-3.0, 15.0)   # always bigger
        _legs.append(((_sx, _sy), {_me: _mine, _decoy: _wander,
                                   _idle: (0.01, 0.0)}))
    assert pick_me(_legs) == _me, hex(pick_me(_legs) or 0)
    assert pick_me([_legs[0]]) is None, "one leg cannot identify anyone"

    # A blank position read must not throw the calibration away: the bot goes
    # silent until someone notices and restarts it. Coast, then give up.
    class _Blind(MemoryEyes):
        def __init__(self):
            self.me, self.basis = 0x1000, [[1.0, 0.0], [0.0, 1.0]]
            self.units, self.chasing, self.engaged_since = [], None, None
            self.ignored = {}
            self.mode, self.misses, self.hot = "chasing", 0, None
            import threading
            self.lock = threading.Lock()

        def _positions(self, _):
            return {}                          # every read comes back empty

    blind = _Blind()
    for i in range(MEM_LOST_FRAMES):
        blind.target(1.0 + i)
        assert blind.me and blind.mode == "lost", (i, blind.mode)
    blind.target(99.0)                         # one miss too many: unit is gone
    assert blind.me is None and blind.mode == "no unit", blind.mode

    # Giving up on a target that will not die. Without this the bot parks on the
    # spot swinging forever, which is indistinguishable from a hung bot.
    assert not stale_target(5.0, None)                   # nothing engaged
    assert not stale_target(5.0, 0.0, limit=8.0)         # still within its time
    assert stale_target(9.0, 0.0, limit=8.0)             # long enough, drop it
    assert MEM_IGNORE_S > MEM_ENGAGE_MAX_S, "must stay ignored longer than tried"

    # Two monsters at nearly the same distance: whichever is chosen must be kept
    # while it stays close, or the bot walks left, right, left, right as they
    # swap which is nearer. TARGET_SWITCH is how much better a rival must be.
    a_closer = [("monster", 1, 10.0, 0.0, 0.0), ("monster", 2, 10.4, 0.0, 0.0)]
    b_closer = [("monster", 1, 10.4, 0.0, 0.0), ("monster", 2, 10.0, 0.0, 0.0)]
    assert nearest_monster(a_closer, 0.0, 0.0)[0][0] == 1
    assert nearest_monster(b_closer, 0.0, 0.0)[0][0] == 2   # bare pick flaps
    held_d, rival_d = 10.4, 10.0                            # holding 1, 2 nearer
    assert not rival_d < held_d * TARGET_SWITCH, "a 4% gain must not switch"
    assert 3.0 < 10.4 * TARGET_SWITCH, "but a much nearer one still wins"

    polled = []
    assert toggle_key_hit(lambda vk: polled.append(vk) or 1)
    assert polled == [0x23]

    class TogglePad:
        def __init__(self):
            self.calls = []

        def stick(self, sx, sy, attack):
            self.calls.append((sx, sy, attack))

    class TogglePets:
        def __init__(self):
            self.resets = 0

        def reset(self):
            self.resets += 1

    toggle_pad, toggle_pets = TogglePad(), TogglePets()
    paused = toggle_running(True, toggle_pad, toggle_pets,
                            wake=lambda pad: pad.calls.append("wake"))
    assert not paused and toggle_pad.calls == [(0.0, 0.0, False), "wake"]
    paused = toggle_running(paused, toggle_pad, toggle_pets,
                            wake=lambda pad: pad.calls.append("wake"))
    assert paused and toggle_pad.calls[-1] == (0.0, 0.0, False)
    assert toggle_pad.calls.count("wake") == 1 and toggle_pets.resets == 2
    img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(img, (150, 60), 4, (0, 0, 255), -1)   # far, up-right
    cv2.circle(img, (120, 100), 4, (0, 0, 255), -1)  # near, right
    cv2.circle(img, (40, 40), 4, (255, 0, 0), -1)    # blue, must be ignored
    # Mushroom art, measured off a real minimap: desaturated pink, and an occluded
    # cap is a dot-sized sliver -- only the colour tells these from a monster.
    cv2.circle(img, (60, 150), 20, (82, 93, 176), -1)   # whole cap
    cv2.circle(img, (30, 60), 4, (82, 93, 176), -1)     # sliver of one
    cv2.circle(img, (150, 170), 4, (0, 0, 255), -1)  # two monsters touching -> one
    cv2.circle(img, (157, 170), 4, (0, 0, 255), -1)  # fat contour, must still count

    cv2.circle(img, (105, 95), 6, (255, 255, 255), -1)  # player arrow, near centre
    cv2.circle(img, (10, 190), 9, (255, 255, 255), -1)  # other white UI, farther off

    dots = find_red_dots(img)
    assert len(dots) == 3, dots       # 2 singles + the merged pair, no mushrooms
    x, y, _ = nearest(dots, 100, 100)
    assert abs(x - 120) < 3 and abs(y - 100) < 3, (x, y)

    # pick_target: origin is the box centre, whatever colour the marker is, and a
    # dot sitting on it is never the target.
    cv2.circle(img, (104, 104), 4, (0, 0, 255), -1)    # right on the centre
    (px, py), targetable, chosen = pick_target(img)
    assert (px, py) == (100, 100), (px, py)            # 200x200 image
    assert all((d[0] - px) ** 2 + (d[1] - py) ** 2 > CONCEAL_PX ** 2
               for d in targetable), targetable
    # The dot on the centre is concealed and dropped; the next nearest is chased.
    assert abs(chosen[0] - 120) < 3 and abs(chosen[1] - 100) < 3, chosen
    assert all(d[0] != 104 for d in targetable), targetable

    # Another player's pet uses the same red dot as a monster, but stays beside
    # that player's small white dot. Confirm the pair over several frames so a
    # monster merely crossing a player is not discarded immediately.
    pet_img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(pet_img, (50, 80), 4, (255, 255, 255), -1)  # other player
    cv2.circle(pet_img, (62, 80), 4, (0, 0, 255), -1)      # their pet
    cv2.circle(pet_img, (150, 80), 4, (0, 0, 255), -1)     # real monster
    assert len(find_white_players(pet_img)) == 1
    pets = PetFilter()
    for _ in range(PET_CONFIRM_FRAMES - 1):
        _, before, chosen = pick_target(pet_img, pets)
        assert len(before) == 2 and chosen[0] < 100, (before, chosen)
    _, after, chosen = pick_target(pet_img, pets)
    assert len(after) == 1 and chosen[0] > 100, (after, chosen)

    far_pet_img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(far_pet_img, (50, 80), 4, (255, 255, 255), -1)
    cv2.circle(far_pet_img, (80, 80), 4, (0, 0, 255), -1)   # pet moved 30px away
    cv2.circle(far_pet_img, (150, 80), 4, (0, 0, 255), -1)
    _, far_after, chosen = pick_target(far_pet_img, pets)
    assert len(far_after) == 1 and chosen[0] > 100, (far_after, chosen)

    sx, sy = stick_vector(x - 100, y - 100, 100)
    assert sx > 0.95 and abs(sy) < 0.05, (sx, sy)      # push right, full tilt
    sx, sy = stick_vector(0, -50, 100)
    assert sy > 0.95 and abs(sx) < 0.05, (sx, sy)      # up = +y
    sx, sy = stick_vector(3, -4, 100)                  # near target, still full
    assert abs((sx * sx + sy * sy) ** 0.5 - 1.0) < 0.01, (sx, sy)

    # Login screens. Built at an odd size on purpose: every coordinate is a
    # fraction, so the flow must work at a resolution nobody measured.
    def blue(img, btn):
        h, w = img.shape[:2]
        bw, bh = int(w * btn[2]), int(h * 0.036)   # each button at its real width
        x, y = int(w * btn[0]), int(h * btn[1])
        cv2.rectangle(img, (x - bw // 2, y - bh // 2), (x + bw // 2, y + bh // 2),
                      (232, 168, 79), -1)          # the game's button blue

    sky = np.zeros((432, 768, 3), np.uint8)
    sky[:] = (200, 150, 90)                        # bright blue sky, and
    blue(sky, PLAY_BTN)                            # blue skill icons below
    blue(sky, OK_BTN)                              # a blue thing up in the sky
    assert login_screen(sky) is None, "gameplay must never look like a login screen"

    disc = np.zeros((432, 768, 3), np.uint8)
    disc[:] = (255, 255, 255)                      # server table behind the modal
    cv2.rectangle(disc, (int(768 * 0.40), int(432 * 0.06)),
                  (int(768 * 0.60), int(432 * 0.18)), (69, 51, 49), -1)
    blue(disc, OK_BTN)
    blue(disc, CONNECT_BTN)                        # both are on screen at once
    assert login_screen(disc) == "disconnected", "the modal must be handled first"

    srv = np.zeros((432, 768, 3), np.uint8)
    srv[:] = (255, 255, 255)
    blue(srv, CONNECT_BTN)
    assert login_screen(srv) == "server"

    # The server list is sorted by ping and reorders between sessions, so SEA has
    # to be found by its label. Drop a fake label into an arbitrary row and check
    # the click follows it there rather than going to a fixed position.
    label = np.zeros((13, 96, 3), np.uint8)
    cv2.putText(label, "SEA", (2, 11), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                (60, 55, 55), 1, cv2.LINE_AA)
    scale = 768 / SEA_REF_W
    small = cv2.resize(label, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    row_y, row_x = 300, 250                      # third row down, nothing special
    srv[row_y:row_y + small.shape[0], row_x:row_x + small.shape[1]] = small
    found = find_sea_row(srv, label)
    assert found is not None, "SEA label must be found wherever the row sits"
    assert abs(found[1] - (row_y + small.shape[0] / 2)) < 3, found

    chars = np.zeros((432, 768, 3), np.uint8)
    chars[:] = (53, 36, 28)                        # the dark character backdrop
    cv2.circle(chars, (int(768 * 0.42), int(432 * 0.60)), 40, (250, 250, 250), -1)
    blue(chars, PLAY_BTN)
    # Regression, found the hard way on a live disconnect: Connect and Play sit
    # 0.033 apart vertically, and the character's own bright armour can sit under
    # the white-panel probe, so this screen read as "server" and the flow stalled.
    assert login_screen(chars) == "character", "Play must not be taken for Connect"
    assert find_blue_button(chars, CONNECT_BTN) is None, "widths must separate them"

    # The clicks themselves: right buttons, right order, window offset applied.
    class FakeWin:
        left, top, width, height = 100, 50, 768, 432

    clicks = []
    def rec(x, y):
        clicks.append((x, y))

    assert reconnect_step(disc, FakeWin, rec, settle=0) == "disconnected"
    assert clicks == [(100 + 768 * OK_BTN[0], 50 + 432 * OK_BTN[1])], clicks
    clicks.clear()
    assert reconnect_step(srv, FakeWin, rec, settle=0, sea_template=label) == "server"
    assert clicks == [(100 + found[0], 50 + found[1]),
                      (100 + 768 * CONNECT_BTN[0], 50 + 432 * CONNECT_BTN[1])], clicks

    # No SEA row means no click at all: joining whichever region happens to sit in
    # that row today is worse than sitting on the screen and saying so.
    blank = srv.copy()
    blank[row_y:row_y + small.shape[0], row_x:row_x + small.shape[1]] = 255
    clicks.clear()
    assert reconnect_step(blank, FakeWin, rec, settle=0,
                          sea_template=label) == "server: SEA row not found"
    assert clicks == [], clicks
    clicks.clear()
    assert reconnect_step(chars, FakeWin, rec, settle=0) == "character"
    assert clicks == [(100 + 768 * PLAY_BTN[0], 50 + 432 * PLAY_BTN[1])], clicks
    clicks.clear()
    assert reconnect_step(sky, FakeWin, rec, settle=0) is None
    assert clicks == [], "gameplay must never move the mouse"

    # ArduinoPad wire format, no board attached
    pad = ArduinoPad.__new__(ArduinoPad)
    sent = []
    pad.ser = type("S", (), {"write": lambda _, b: sent.append(b),
                             "readline": lambda _: b"OK"})()
    pad.last = None
    pad.stick(0.0, 1.0)             # stick up -> HID Y negative
    pad.stick(0.0, 1.0)             # repeat must not re-send
    pad.stick(-1.0, 0.0, True)      # move + attack down
    pad.stick(-1.0, 0.0, False)     # stick unchanged -> only the release
    assert sent == [b"L0,-32767\n", b"U4\n",
                    b"L-32767,0\n", b"D4\n",
                    b"U4\n"], sent

    sent.clear()
    for key in BUFF_SEQUENCE:
        pad.tap_dpad(key, 0)
    assert sent == [b"V0\n", b"V-1\n", b"V6\n", b"V-1\n",
                    b"V4\n", b"V-1\n", b"V2\n", b"V-1\n"], sent

    sent.clear()
    pad.tap_button("y")           # by name
    pad.tap_button(3)             # same button by index
    assert sent == [b"B3\n", b"B3\n"], sent

    print("demo ok")


def snap(path="minimap_snap.png"):
    """Dump what the bot sees: captured region, detections circled, centre marked."""
    import mss
    win = find_window()
    with mss.mss() as sct:
        img = np.array(sct.grab(minimap_region(win)))[:, :, :3].copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2  # the game pins the character here; see pick_target
    print(f"  player assumed at box centre ({cx},{cy}) -- the cross must sit on the "
          f"character marker, whatever colour it is")
    for x, y, wpx in find_red_dots(img):
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        hid = d <= CONCEAL_PX
        cv2.circle(img, (int(x), int(y)), 8, (0, 0, 255) if hid else (0, 255, 0), 1)
        print(f"  dot at ({x:6.1f},{y:6.1f}) width {wpx:5.1f} dist {d:6.1f}"
              f"{'  REJECTED: under player marker' if hid else ''}")
    cv2.drawMarker(img, (cx, cy), (255, 255, 0), cv2.MARKER_CROSS, 14, 1)
    cv2.circle(img, (cx, cy), CONCEAL_PX, (255, 255, 0), 1)

    cv2.imwrite(path, img)
    print(f"{w}x{h} region -> {path}")


def draw_tracking(img, pet_filter=None, target_lock=None):
    """Annotate a minimap grab in place with what the bot would do. Returns a label.

    green = targetable, orange = confirmed pet, red = ignored under the arrow,
    cyan = chosen target, magenta arrow = the stick vector that would be sent.
    """
    (cx, cy), dots, dot = pick_target(img, pet_filter, target_lock)
    ipx = (int(cx), int(cy))
    for x, y, _ in find_red_dots(img):
        hidden = (x - cx) ** 2 + (y - cy) ** 2 <= CONCEAL_PX ** 2
        kept = any((x - d[0]) ** 2 + (y - d[1]) ** 2 < 1 for d in dots)
        colour = (0, 0, 255) if hidden else ((0, 255, 0) if kept else (0, 165, 255))
        cv2.circle(img, (int(x), int(y)), 7, colour, 1)
    cv2.circle(img, ipx, CONCEAL_PX, (255, 255, 0), 1)
    cv2.drawMarker(img, ipx, (255, 255, 0), cv2.MARKER_CROSS, 12, 1)
    if dot is None:
        state = "target concealed" if target_lock and target_lock.concealed else "no target"
        return f"{len(dots)} dots  {state}"
    tgt = (int(dot[0]), int(dot[1]))
    cv2.line(img, ipx, tgt, (0, 255, 255), 1)
    cv2.circle(img, tgt, 10, (0, 255, 255), 2)
    sx, sy = stick_vector(dot[0] - cx, dot[1] - cy, 1)
    # stick vector as an arrow from the player, y flipped back to screen sense
    cv2.arrowedLine(img, ipx, (int(cx + sx * 30), int(cy - sy * 30)),
                    (255, 0, 255), 2, tipLength=0.3)
    return f"{len(dots)} dots  dist {((dot[0] - cx) ** 2 + (dot[1] - cy) ** 2) ** 0.5:.0f}"


def watch(scale=2):
    """Live view of what the vision layer sees. Read-only -- drives nothing.

    Safe to run in a second terminal while the bot works: two mss grabs of the
    same region do not collide. q or ESC quits.
    """
    import mss
    win = find_window()
    title = "spiritvale tracking -- q to quit"
    pet_filter = PetFilter()
    target_lock = TargetLock()
    print(f"watching {minimap_region(win)}  (q or ESC in the window to quit)")
    last, fps = time.time(), 0.0
    with mss.mss() as sct:
        while True:
            img = np.array(sct.grab(minimap_region(win)))[:, :, :3].copy()
            label = draw_tracking(img, pet_filter, target_lock)
            view = cv2.resize(img, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_NEAREST)
            now = time.time()
            fps = 0.9 * fps + 0.1 / max(now - last, 1e-6)
            last = now
            cv2.putText(view, f"{label}  {fps:.0f}fps", (6, 16),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow(title, view)
            if cv2.waitKey(max(1, int(1000 / LOOP_HZ))) in (27, ord("q")):
                break
    cv2.destroyAllWindows()


def relogin(dry=False):
    """Handle whatever login screen is showing, once. --dry looks without clicking."""
    import mss
    win = find_window()
    with mss.mss() as sct:
        img = np.array(sct.grab(window_region(win)))[:, :, :3]
    screen = login_screen(img)
    print(f"screen: {screen or 'not a login screen (gameplay, or something new)'}")
    if screen is None or dry:
        if screen and dry:
            h, w = img.shape[:2]
            if screen == "server":
                sea = find_sea_row(img)
                where = (f"SEA row at ({win.left + sea[0]:.0f},{win.top + sea[1]:.0f})"
                         if sea else "SEA row NOT FOUND -- would not click")
            else:
                f = OK_BTN if screen == "disconnected" else PLAY_BTN
                where = f"({win.left + w * f[0]:.0f},{win.top + h * f[1]:.0f})"
            print(f"  would click {where}")
        return
    print(f"handled: {reconnect_step(img, win)}")


def probe(hold=0.4, gap=2.0):
    """Press every X360 button in turn, naming each. Watch which one buffs."""
    import vgamepad as vg
    pad = vg.VX360Gamepad()
    names = [n for n in dir(vg.XUSB_BUTTON) if n.startswith("XUSB_GAMEPAD_")]
    print(f"focus the game NOW -- 3s, then {len(names)} buttons, {gap}s apart")
    time.sleep(3)
    try:
        for sx, sy in ((0.0, WAKE_AMP), (0.0, -WAKE_AMP), (0.0, 0.0)):
            pad.left_joystick_float(sx, sy)
            pad.update()
            time.sleep(WAKE_STEP_S)
        time.sleep(WAKE_SETTLE_S)
        for n in names:
            b = getattr(vg.XUSB_BUTTON, n)
            print(f"  {n.replace('XUSB_GAMEPAD_', '')}")
            pad.press_button(b)
            pad.update()
            time.sleep(hold)
            pad.release_button(b)
            pad.update()
            time.sleep(gap)
        # Triggers are axes, not buttons -- probe them too.
        for label, setter in (("LEFT_TRIGGER", pad.left_trigger_float),
                              ("RIGHT_TRIGGER", pad.right_trigger_float)):
            print(f"  {label}")
            setter(1.0)
            pad.update()
            time.sleep(hold)
            setter(0.0)
            pad.update()
            time.sleep(gap)
    finally:
        pad.reset()
        pad.update()
    print("done -- tell me which name was on screen when a buff cast")


def buff_test(port=None, hold=BUFF_HOLD_S, gap=BUFF_GAP_S):
    """Fire the buff sequence once so timing can be tuned without a 60s wait."""
    pad = ArduinoPad(port) if port else VirtualPad()
    print(f"focus the game NOW -- 3s, then {' '.join(BUFF_SEQUENCE)} "
          f"(hold {hold}s, gap {gap}s)")
    time.sleep(3)
    try:
        wake_controller(pad)
        for key in BUFF_SEQUENCE:
            print(f"  {key}")
            pad.tap_dpad(key, hold)
            time.sleep(gap)
    finally:
        pad.close()
    print("done -- if nothing cast, raise hold/gap")


TAP_HELP = ("button 0-15 or a/b/x/y, d-pad u/d/l/r, stick lx/ly/rx/ry "
            "(prefix - for the other direction, e.g. -lx = left)")
_DIRS = {"u": "up", "d": "down", "l": "left", "r": "right"}


def tap_one(pad, token):
    """Fire one control named the way TAP_HELP describes it. True if understood."""
    token = token.strip().lower()
    sign = -1 if token.startswith("-") else 1
    token = token.lstrip("-+")
    if token in _DIRS:
        pad.tap_dpad(_DIRS[token], 0.2)
    elif token in ("lx", "ly", "rx", "ry"):
        # Full deflection and back. The wizard asks for one direction at a time,
        # so each axis needs both signs: -lx is left, lx is right.
        axis = "L" if token[0] == "l" else "R"
        v = 32767 * sign
        x, y = (v, 0) if token[1] == "x" else (0, v)
        pad._cmd(f"{axis}{x},{y}")
        time.sleep(0.4)
        pad._cmd(f"{axis}0,0")
    elif token in ArduinoPad.FACE:
        pad.tap_button(token)
    elif token.isdigit():
        pad.tap_button(int(token))
    else:
        return False
    return True


def press_repl(port="auto"):
    """Tap one control at a time, on demand.

    Steam has no mapping for an unknown HID pad, so it runs a setup wizard that
    asks you to press each control in turn -- and nothing reaches the game until
    that is done. Nobody can press a button on a board with no buttons, hence
    this: type what the wizard is waiting for. One connection stays open for the
    whole run, so the board never resets mid-wizard.
    """
    pad = ArduinoPad(port)
    print(f"{TAP_HELP}\nblank line quits")
    try:
        while True:
            s = input("> ").strip()
            if not s:
                break
            if not tap_one(pad, s):
                print(f"  ? expected {TAP_HELP}")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        pad.close()
        print("\nclosed")


def stick_test(port=None, seconds=12):
    """Blind pad check: walk a circle. Character moves -> pad fine, vision is the bug."""
    pad = ArduinoPad(port) if port else VirtualPad()
    print("focus the game NOW; walking a circle for", seconds, "s")
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            a = (time.time() - t0) * 1.2
            sx, sy = np.cos(a), np.sin(a)
            pad.stick(float(sx), float(sy), False)
            print(f"stick {sx:+.2f},{sy:+.2f}   ", end="\r")
            time.sleep(1 / LOOP_HZ)
    except KeyboardInterrupt:
        pass  # cutting the test short is normal, not a crash
    finally:
        pad.close()
        print("\ndone")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--snap" in sys.argv:
        snap()
    elif "--watch" in sys.argv:
        watch()
    elif "--relogin" in sys.argv:
        relogin("--dry" in sys.argv)
    elif "--probe" in sys.argv:
        probe()
    elif "--press" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        press_repl(sys.argv[i + 1] if i >= 0 else "auto")
    elif "--buff" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        j = sys.argv.index("--buff")
        rest = [a for a in sys.argv[j + 1:] if not a.startswith("--")]
        hold = float(rest[0]) if len(rest) > 0 else BUFF_HOLD_S
        gap = float(rest[1]) if len(rest) > 1 else BUFF_GAP_S
        buff_test(sys.argv[i + 1] if i >= 0 else None, hold, gap)
    elif "--test" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        stick_test(sys.argv[i + 1] if i >= 0 else None)
    else:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        main(sys.argv[i + 1] if i >= 0 else None)
